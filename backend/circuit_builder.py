"""
Isolated module for invoking Circuit_Builder headlessly via the `claude` CLI.

This is the ONLY place that knows about the `claude -p ...` invocation contract
(flags, output format, permission scoping). If that contract changes, only this
file should need to change.

Invocation contract, validated by hand on this machine (2026-08-19):
  - `claude --agent Circuit_Builder -p "<prompt>"` routes the whole headless
    session to the custom Circuit_Builder subagent (defined at
    ~/.claude/agents/Circuit_Builder.md) instead of relying on default
    intent-based routing.
  - Headless sessions have NO TTY, so nothing can approve interactive
    permission prompts. Broad bypass flags (--dangerously-skip-permissions,
    --permission-mode bypassPermissions, or a bare "Bash" in --allowedTools)
    get hard-denied by this machine's "auto mode" safety classifier as
    over-broad. What *does* work reliably is a narrow, explicit
    --allowedTools allow-list scoped to exactly the commands Circuit_Builder's
    documented workflow needs (xschem, xvfb-run, ngspice, and a handful of
    read-only/file-management commands), plus Read/Glob/Grep, plus
    --add-dir for the PDK path (Circuit_Builder needs to Read *.sym files
    under $PDK_ROOT to get pin geometry right).
  - Write/Edit MUST be path-scoped, not bare. A bare "Write"/"Edit" entry in
    --allowedTools grants write access to the ENTIRE filesystem the process
    user can reach, not just cwd - confirmed the hard way when an earlier
    version of this code (bare "Write"/"Edit") let a Circuit_Builder run
    silently patch a paragraph into THIS FILE's prompt template, outside its
    assigned run directory, without being asked to. The fix, verified by hand:
    a *relative*, non-leading-slash glob like "Write(**)"/"Edit(**)" resolves
    relative to the subprocess's cwd (which we always set to the run
    directory), so it permits in-run-dir writes and denies anything
    referenced by an absolute path elsewhere - exactly what we want since
    every run gets its own fresh directory and nothing else should be
    touched. Do not change this back to a bare "Write"/"Edit".
  - `--output-format stream-json --verbose` emits one JSON event per line as
    the session progresses (assistant text/tool-use, tool results, a final
    `type":"result"` object) - unlike `--output-format json`, which buffers
    everything and prints nothing until the whole session ends. Streaming is
    what lets us tail a live, human-readable log file while a run is still
    in progress, and lets a cancel request actually interrupt a run instead
    of only being able to kill-and-lose-everything.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from models_contract import (
    MODEL_LABELS,
    MODEL_TYPES,
    RNM_RULES,
    VERILOG_RULES,
    augmented_path,
    build_models_prompt_section,
    check_models_in_summary,
    model_guidance,
    normalize_arch_blocks,
    toolchain_status,
    validate_arch_model_request,
)
from topologies import extra_fields_target_dict, format_extra_field_lines, topology_display_name

CLAUDE_BIN = "claude"
PDK_ROOT = Path.home() / ".volare"

# Narrow, command-scoped allow-list. Deliberately does NOT include a bare
# "Bash", "Write", "Edit", or any permission-bypass flag - see module
# docstring. Write/Edit are scoped to cwd (the run directory) via the
# relative "(**)" glob, NOT given as bare tool names.
ALLOWED_TOOLS = [
    "Write(**)",
    "Edit(**)",
    "Read",
    "Glob",
    "Grep",
    "Bash(xschem *)",
    "Bash(xvfb-run *)",
    "Bash(ngspice *)",
    # Behavioral-model verification toolchain (cycle 2): compile Verilog-A to
    # OSDI, compile/run Verilog+RNM testbenches. All three only read/write
    # within cwd (the run directory) the way Circuit_Builder is told to use
    # them; openvaf resolves via the project-local tools/bin prepended to
    # PATH below (no sudo install on this machine).
    "Bash(openvaf *)",
    "Bash(iverilog *)",
    "Bash(vvp *)",
    "Bash(ls *)",
    "Bash(ls)",
    "Bash(cat *)",
    "Bash(which *)",
    "Bash(printenv *)",
    "Bash(mkdir *)",
    "Bash(find *)",
    "Bash(grep *)",
    "Bash(cp *)",
    "Bash(mv *)",
    "Bash(pwd)",
]

# Defense in depth: deny writes to the paths that actually matter (this
# tool's own code, the agent/permission config, the installed toolchain, the
# PDK) so a future change to ALLOWED_TOOLS (e.g. someone adding a bare
# "Write" back) doesn't silently reopen write access to them.
#
# CAUTION (learned from the first real cycle-3 calibration run): the earlier
# blanket denies "Write(/**)"/"Write(~/**)" matched EVERY absolute path -
# including the run directory itself, because the Write/Edit tools receive
# absolute file paths and every run dir lives under both / and ~. Deny rules
# override the relative "Write(**)" allow, so ALL writes were denied and
# Circuit_Builder had to work around it by authoring files through the
# xschem Tcl interpreter. Deny rules must therefore be TARGETED, never
# filesystem-wide.
_BASE = Path(__file__).resolve().parent.parent  # the analog-spec-tool checkout


def disallowed_tools() -> list[str]:
    """Computed per-run (not a module constant) because the libraries roots
    come from the working-dir setting, resolved at request time - filed
    library cells must stay write-protected wherever the user points the
    working dir, including previous locations."""
    from settings import all_libraries_roots  # avoid import cycle at module load

    denies = [
        f"Write({_BASE}/backend/**)",
        f"Edit({_BASE}/backend/**)",
        f"Write({_BASE}/frontend/**)",
        f"Edit({_BASE}/frontend/**)",
        f"Write({_BASE}/tools/**)",
        f"Edit({_BASE}/tools/**)",
        "Write(~/.claude/**)",
        "Edit(~/.claude/**)",
        "Write(~/.volare/**)",
        "Edit(~/.volare/**)",
    ]
    for root in all_libraries_roots():
        denies += [f"Write({root}/**)", f"Edit({root}/**)"]
    return denies

DEFAULT_TIMEOUT_S = 2400  # a real netlist+sim+render run (incl. testbench debugging) can take 15-30+ minutes
POLL_INTERVAL_S = 1.0  # how often the read loop checks cancel/timeout when no output is ready

# Single-deliverable run modes (RunSpec.deliverable). "full" = the original
# design+simulate(+models) flow, unchanged. The others produce ONLY the named
# deliverable; symbol/model-only runs skip transistor-level design entirely
# and are much cheaper/faster, which the timeouts (and the run-record
# metadata main.py stores) reflect.
DELIVERABLES = ("full", "schematic_only", "symbol", *MODEL_TYPES, "arch_model", "arch_stitch")
# Which modes are cheap/fast (no SPICE verification suite, no schematic
# drawing/rendering) - surfaced to the UI as a "light run" label. arch_model
# is model-only too but spans many blocks, so it is not labeled light.
LIGHT_DELIVERABLES = ("symbol", *MODEL_TYPES)
_DELIVERABLE_TIMEOUTS_S = {
    "full": DEFAULT_TIMEOUT_S,
    "schematic_only": 1800,  # design+draw+render+DC sanity, no verification suite
    "symbol": 600,           # one .sym plus a netlist smoke check
    "veriloga": 1200,        # author + openvaf compile + ngspice self-check TB
    "verilog": 1200,
    "rnm": 1200,
    "arch_model": 1800,      # one model per included block + wired top + TB
    "arch_stitch": 1200,     # one top-level .sch instantiating filed symbols + netlist + PNG
}


def timeout_for(deliverable: str | None) -> int:
    return _DELIVERABLE_TIMEOUTS_S.get(deliverable or "full", DEFAULT_TIMEOUT_S)


@dataclass
class CircuitBuilderResult:
    status: str  # "success" | "failed"
    reason: str = ""  # human-readable reason when status == "failed"
    summary: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> filename in run_dir


def _write_project_xschemrc(run_dir: Path) -> None:
    # Also put the tool's hierarchical design-library trees (libraries/
    # <library>/<cell>/<views> - see backend/libraries.py) on
    # XSCHEM_LIBRARY_PATH: xschem "libraries" are just directories on that
    # path, so cells filed by previous runs are browsable and their symbols
    # instantiable (as `<library>/<cell>/<cell>.sym`) from any run's session.
    # All roots (current working dir + previous ones - see settings.py) are
    # appended, so cells filed before a working-dir change stay instantiable.
    from settings import all_libraries_roots  # request-time resolution, no restart needed

    lib_lines = "".join(
        f"append XSCHEM_LIBRARY_PATH :{root}\n" for root in all_libraries_roots()
    )
    (run_dir / "xschemrc").write_text(
        "# Project-local xschemrc: pull in the sky130 PDK xschem libraries.\n"
        "if {![info exists env(PDK_ROOT)]} { set env(PDK_ROOT) $env(HOME)/.volare }\n"
        "source $env(PDK_ROOT)/sky130A/libs.tech/xschem/xschemrc\n"
        "# SPB design libraries (hierarchical lib/cell/view directory tree).\n"
        + lib_lines
    )


def build_prompt(spec: dict[str, Any]) -> str:
    """Dispatch to a topology-specific prompt builder.

    "nmos_current_mirror" (the default, for backward compatibility with the
    only flow this tool originally supported) gets the detailed,
    hand-validated prompt below that pins down exact device names, pin
    names, and a sizing formula. Every other topology - the PHY building
    blocks in the spec form's dropdown (see topologies.py) - gets the
    generic prompt in build_generic_prompt(), which hands the topology name
    and whatever spec numbers were given to Circuit_Builder and trusts its
    own topology/sizing judgment, rather than this tool pretending to know
    the internal structure of a PLL, CDR, DFE, etc.
    """
    topology = spec.get("topology", "nmos_current_mirror")

    # Single-deliverable modes (RunSpec.deliverable): each produces ONLY the
    # named deliverable instead of the full design+simulate+models flow.
    # "full" (or absent - every pre-feature spec) falls through to the
    # original prompts below, unchanged.
    deliverable = spec.get("deliverable") or "full"
    if deliverable == "schematic_only":
        return _build_schematic_only_prompt(spec)
    if deliverable == "symbol":
        return _build_symbol_prompt(spec)
    if deliverable in MODEL_TYPES:
        return _build_model_only_prompt(spec, deliverable)
    if deliverable == "arch_model":
        return _build_arch_model_prompt(spec)
    if deliverable == "arch_stitch":
        return _build_arch_stitch_prompt(spec)

    if topology == "nmos_current_mirror":
        prompt = _build_current_mirror_prompt(spec)
    else:
        prompt = _build_generic_prompt(spec)

    # Behavioral-model contract (cycle 2): when the user ticked any model
    # checkbox, append the per-type deliverables/restrictions/verification
    # section from models_contract.py to EITHER prompt branch. Appended after
    # the base prompt (which ends by describing summary.json + the final
    # reply), so remind the model that summary.json - now including the
    # "models" key - stays the last file written.
    requested = spec.get("models") or []
    if requested:
        prompt += build_models_prompt_section(topology, list(requested))
        prompt += (
            "\nSequencing note: do all behavioral-model authoring and "
            "verification BEFORE writing summary.json, so its \"models\" key "
            "reflects what actually happened - summary.json remains the very "
            "last file you write, and your final plain-English reply should "
            "mention each model's verification outcome.\n"
        )
    return prompt


def _vdd_headroom_note(vdd_v: float) -> str:
    """Soft low-supply feasibility note routed into the prompt (the hard
    >1.98 V bound is rejected in main.py's validators before we get here).
    sky130 pfet_01v8 |Vt| is high (~0.6-0.9 V depending on flavor), so below
    ~1.2 V most topologies are headroom-starved without lvt devices."""
    if vdd_v >= 1.2:
        return ""
    return (
        f"\nHEADROOM WARNING: VDD = {vdd_v} V is low for sky130's 1.8V device family - "
        "pfet_01v8 |Vt| is ~0.6-0.9 V, so expect to need lvt device flavors and reduced "
        "stacking (few or no cascodes). State explicitly what you did about this.\n"
    )


def _build_current_mirror_prompt(spec: dict[str, Any]) -> str:
    label = spec.get("label") or "unlabeled run"
    iref_ua = spec["iref_ua"]
    ratio_out = spec["ratio_out"]
    ratio_ref = spec["ratio_ref"]
    vdd_v = spec["vdd_v"]
    w_ref_um = spec["w_ref_um"]
    l_ref_um = spec["l_ref_um"]
    corner = spec["corner"]
    temp_c = spec.get("temp_c", 27)
    phy_type = spec.get("phy_type", "ser-des")
    selected_block = spec.get("selected_block", "")
    w_mirror_um = round(w_ref_um * (ratio_out / ratio_ref), 4)
    chosen_architecture = spec.get("chosen_architecture")
    chosen_rationale = spec.get("chosen_architecture_rationale")

    # Same idea as _build_generic_prompt's chosen_architecture handling: a
    # plain two-transistor mirror is itself just one candidate architecture
    # (vs. cascode, Wilson, regulated-cascode, etc.) for this topology
    # category, so Circuit_Researcher may have recommended something other
    # than the tool's original hard-coded default. When that happened,
    # swap in the chosen architecture instead of forcing the fixed
    # two-transistor description; otherwise (no research, or research
    # skipped) keep the exact original prompt text unchanged.
    tb_step = _testbench_step(
        "current_mirror_tb",
        spec.get("testbench_style", "spice"),
        f"The testbench must set VDD = {vdd_v} V, Iref = {iref_ua} uA, use the "
        f"`{corner}` corner, set the simulation temperature with `.temp {temp_c}`, "
        "and run a `.op` (operating point) analysis to measure: "
        "the actual current into M2's drain (IOUT), M2's drain-source voltage "
        "(a proxy for output compliance/headroom), and M1's gate-source voltage.",
    )
    tb_sch_json_line = (
        '\n     "testbench_sch_file": "current_mirror_tb.sch",'
        if spec.get("testbench_style") == "xschem"
        else ""
    )

    if chosen_architecture:
        rationale_clause = f" Circuit_Researcher's rationale: {chosen_rationale}" if chosen_rationale else ""
        topology_section = f"""## Topology
Circuit_Researcher compared current-mirror architectures (e.g. simple
two-transistor, cascode, Wilson, regulated-cascode) for this spec and the
user selected: **{chosen_architecture}**.{rationale_clause}
Build this specific architecture. Only fall back to the plain
two-transistor topology if the chosen one turns out to be clearly
infeasible in sky130 at this supply (e.g. not enough headroom for a
cascode stack) - if so, explain why in your opening remarks and in the
"notes" field of summary.json.
sky130 device to use throughout: `sky130_fd_pr__nfet_01v8` (symbol
`sky130_fd_pr/nfet_01v8.sym`), bulk tied to GND, `nf=1 mult=1` per device
unless you need fingers/mult for a width-rule reason."""
        sizing_adapt_note = (
            " This W-scaling formula is the plain two-transistor mirror's sizing model - if the chosen "
            "architecture needs extra devices (e.g. cascode/Wilson), size those additional devices with your "
            "own judgment and say what you did in \"sizing_note\"."
        )
    else:
        topology_section = """## Topology (fixed for this tool - build exactly this, nothing more)
Standard two-transistor NMOS current mirror, same shape as a known-good
reference design: M1 is diode-connected (gate tied to drain) and carries the
reference current from an ideal DC current source `Iref` pulled up to VDD;
M2 has the same gate/source connection as M1 (gate tied to M1's
gate/drain net, source to GND) and mirrors the current out to an output pin
`IOUT`. Both transistors are `sky130_fd_pr__nfet_01v8` (symbol
`sky130_fd_pr/nfet_01v8.sym`), bulk tied to GND (source), `nf=1 mult=1`."""
        sizing_adapt_note = ""

    return f"""Design, build, and verify an NMOS current mirror in sky130 in the CURRENT
WORKING DIRECTORY ONLY. Do not touch any files outside this directory (in
particular, do NOT touch ~/sky130-projects/current_mirror/ - that is a
different, unrelated project that may be in active use). A project-local
`xschemrc` already exists here sourcing the PDK; use it as-is.

Run label: {label}
PHY Type: {phy_type}
Selected Block: {selected_block if selected_block else "current mirror"}


{topology_section}

## Spec to build against (parameterized - use these exact values, don't
## substitute your own defaults)
- Reference current Iref = {iref_ua} uA
- Mirror ratio (output:reference) = {ratio_out}:{ratio_ref}
- Supply voltage VDD = {vdd_v} V
- Reference device sizing (given directly, not something you should solve
  for): M1 W = {w_ref_um} um, L = {l_ref_um} um
- Mirror device sizing: derive M2's W by scaling M1's W by the ratio, same L
  as M1: M2 W = {w_ref_um} * ({ratio_out}/{ratio_ref}) = {w_mirror_um} um,
  L = {l_ref_um} um. (If a clean single-device W is impractical - e.g. it
  would violate a sky130 nfet_01v8 min/max width rule - use `nf=` fingers or
  `mult=` instead to hit the same effective ratio and say so in notes; but
  first try a plain W scale since that is what this ratio was chosen to
  produce.){sizing_adapt_note}
- Process corner: {corner} (sky130 tt/ff/ss/sf/fs). Use
  `$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice` with this corner
  in any testbench `.lib` include.
- Simulation temperature: {temp_c} C (`.temp {temp_c}` in the testbench -
  ngspice defaults to 27 C otherwise).
{_vdd_headroom_note(vdd_v)}

## What to actually do (all in this directory)
1. State the sizing/biasing choices briefly (per your usual practice).
2. Hand-write `current_mirror.sch` (the xschem schematic) per your standard
   authoring workflow: diode-connected M1, mirror M2, `devices/isource.sym`
   for Iref, `devices/vdd.sym`/`devices/gnd.sym` for rails, `devices/opin.sym`
   labeled IOUT on M2's drain. Give it a short `T {{...}}` title/caption.
3. Netlist it headlessly (`xschem -x -q -n -s current_mirror.sch`) and check
   the resulting connectivity (M1 diode-connected, M2 mirroring, current
   source correctly referenced to VDD) before moving on.
4. Render `current_mirror.sch` to `current_mirror.png` in this directory via
   the standard Xvfb workflow (hide layers 15/17), then Read the PNG and
   visually sanity-check it (no overlapping text, real junction dots where
   nets are meant to join, IOUT clearly labeled) - fix and re-render if not.
{tb_step}
6. Run that testbench in ngspice batch mode (`ngspice -b current_mirror_tb.spice`
   or similar), capture the log, and pull the real simulated numbers out of
   it - do not estimate or hand-calculate these numbers, report exactly what
   ngspice measured. If it does not converge, say so plainly, include the
   ngspice error output, and still complete steps 7-8 with whatever partial
   information you have (do not silently give up). If xschem's netlist wraps
   junction-parasitic params (ad/as/pd/ps/nrd/nrs) in an `expr(...)` form
   ngspice's batch parser rejects, it is fine to hand-write the testbench
   device lines without those params (they default to 0 and don't affect a
   DC .op) rather than getting stuck on it.
7. Save the full ngspice run output to a file named `sim.log` in this
   directory.
8. As the LAST thing you do, write a file named exactly `summary.json` in
   this directory (via the Write tool) containing ONLY a single JSON object
   (no markdown fence, no extra text in that file) with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason if status is "failed"
       (e.g. "ngspice did not converge", "netlist connectivity wrong"),
     "topology": "diode-connected NMOS reference + mirror output",
     "corner": "{corner}",
     "vdd_v": {vdd_v},
     "temp_c": {temp_c},
     "iref_target_ua": {iref_ua},
     "ratio_target": "{ratio_out}:{ratio_ref}",
     "w_ref_um": {w_ref_um},
     "l_ref_um": {l_ref_um},
     "w_mirror_um": <the W you actually used for M2, a number>,
     "sizing_note": "<one sentence, e.g. if you used nf/mult instead of a
       plain W scale>",
     "iref_simulated_ua": <real ngspice-measured Iref, a number, or null if
       sim did not converge>,
     "iout_simulated_ua": <real ngspice-measured IOUT, a number, or null>,
     "ratio_measured": <iout_simulated_ua / iref_simulated_ua as a number, or
       null>,
     "vds_mirror_v": <M2's simulated Vds, a number, or null - this is the
       output compliance/headroom indicator>,
     "vgs_ref_v": <M1's simulated Vgs, a number, or null>,
     "schematic_png": "current_mirror.png",
     "schematic_file": "current_mirror.sch",
     "netlist_file": "current_mirror.spice",
     "testbench_file": "current_mirror_tb.spice",{tb_sch_json_line}
     "sim_log_file": "sim.log",
     "notes": "<any caveats, e.g. non-convergence, a rule violation you
       worked around, a large deviation between target and measured ratio>"
   }}
   Write this file even if something failed along the way - in that case set
   status to "failed", fill in "error", and leave any fields you could not
   measure as null. This file is the single source of truth another program
   will read to display results, so it MUST exist and MUST be valid JSON
   parseable on its own, and its "status" field must honestly reflect
   whether this run produced a usable, verified schematic/netlist - do not
   report "success" if the simulation did not converge or the netlist
   connectivity was wrong.

After writing summary.json, give a brief (3-5 sentence) plain-English
summary of what you built and measured as your final reply.
"""


def _testbench_step(tb_base: str, style: str, what_to_verify: str) -> str:
    """Step-5 text for the two user-selectable testbench styles. Either way
    the testbench is named <circuit>_tb and what ngspice actually runs is
    <circuit>_tb.spice."""
    if style == "xschem":
        return f"""5. Build the testbench as an xschem schematic named `{tb_base}.sch` (same
   authoring/verification workflow as the main schematic): instantiate the
   circuit together with its supplies/sources, and put the sky130 `.lib`
   include (at the right corner), any params, and the analysis/measure
   commands in a `devices/code_shown.sym` text block so they land in the
   netlist. Netlist it headlessly (`xschem -x -q -n -s {tb_base}.sch`) and
   make sure the resulting `{tb_base}.spice` ends up in THIS directory (copy
   it here if xschem wrote it to its default simulations dir). {what_to_verify}"""
    return f"""5. Hand-write a standalone testbench SPICE file named `{tb_base}.spice`
   that instantiates the circuit, includes the sky130 models at the right
   corner, and sets up the analysis. {what_to_verify}"""


def _cell_naming_note(spec: dict[str, Any]) -> str:
    """When the user targeted an existing library cell, the schematic's base
    name must match it so the run's views land as views OF that cell."""
    cell = spec.get("cell")
    if not cell:
        return ""
    return (
        f" Name the schematic exactly `{cell}.sch` - the user chose to file this "
        f"run into the existing library cell `{cell}`, so the base name must match."
    )


def _spec_line(label: str, value: Any, unit: str = "") -> str:
    if value is None or value == "":
        return f"- {label}: not specified - use your own engineering judgment"
    return f"- {label}: {value}{(' ' + unit) if unit else ''}"


def _json_literal(value: Any) -> str:
    """Render a Python value as a JSON literal for embedding in prompt text
    (so example snippets shown to Circuit_Builder are valid JSON, not
    Python's None/True/False repr)."""
    return json.dumps(value)


def _build_generic_prompt(spec: dict[str, Any]) -> str:
    label = spec.get("label") or "unlabeled run"
    topology = spec.get("topology", "")
    display_name = topology_display_name(topology, spec.get("custom_topology"))
    vdd_v = spec["vdd_v"]
    corner = spec["corner"]
    temp_c = spec.get("temp_c", 27)
    phy_type = spec.get("phy_type", "ser-des")
    selected_block = spec.get("selected_block") or ""
    chosen_architecture = spec.get("chosen_architecture")
    chosen_rationale = spec.get("chosen_architecture_rationale")

    if chosen_architecture:
        rationale_clause = f" Circuit_Researcher's rationale: {chosen_rationale}" if chosen_rationale else ""
        topology_clause = f"""## What to build
This is a high-speed PHY (SerDes/wireline) building block: a {display_name}.
Circuit_Researcher already compared candidate architectures for this
category and the user selected: **{chosen_architecture}**.{rationale_clause}
Build this specific architecture. Only deviate from it if it turns out to be
clearly infeasible in sky130 (e.g. a required device/headroom isn't
available at this supply) - if so, explain why in your opening remarks and
in the "notes" field of summary.json, and pick the closest viable
alternative."""
    else:
        topology_clause = f"""## What to build
This is a high-speed PHY (SerDes/wireline) building block: a {display_name}.
This tool does not prescribe an internal topology for this block - use your
own domain expertise to choose a sky130-appropriate topology/sizing that can
plausibly meet the spec below, and briefly state and justify that choice
before you start building."""

    # Topology-specific spec fields (e.g. ref_freq_mhz/loop_bandwidth_mhz for
    # a PLL, transimpedance_gain_kohm/input_capacitance_pf for a TIA) - see
    # topologies.TOPOLOGY_FIELDS for the per-topology field list this is
    # driven from. Falls back to "no topology-specific fields defined for
    # this block yet" for a topology with no schema entry (e.g. "custom"),
    # rather than silently emitting nothing with no explanation.
    extra_field_lines = format_extra_field_lines(topology, spec.get("extra_fields"))
    extra_fields_block = (
        "\n".join(extra_field_lines)
        if extra_field_lines
        else "- (no structured spec fields defined for this topology - see any notes below)"
    )
    target_spec_obj = extra_fields_target_dict(topology, spec.get("extra_fields"))
    target_spec_obj["power_mw"] = spec.get("power_mw")
    target_spec_obj["area_um2"] = spec.get("area_um2")
    target_spec_json = _json_literal(target_spec_obj)

    tb_step = _testbench_step(
        "<circuit>_tb",
        spec.get("testbench_style", "spice"),
        "(`<circuit>` above is your main schematic's base name - e.g. a "
        "`ctle_stage.sch` circuit gets a `ctle_stage_tb` testbench.) "
        f"The testbench must set VDD = {vdd_v} V, use the `{corner}` corner, set the "
        f"simulation temperature with `.temp {temp_c}`, and "
        "run whatever analysis (`.op`, `.ac`, `.tran`, etc.) is actually "
        "appropriate to verify the spec values given above (e.g. `.ac` for a "
        "gain/bandwidth target, `.tran` for a data-rate or jitter target). If a "
        "given spec value isn't practically verifiable in this environment "
        "(e.g. long-term jitter, full-rate serial testing), verify what you "
        "reasonably can and say plainly what you could not verify and why, "
        "rather than fabricating a number."
        + (
            " For this bandgap reference specifically: also run a DC temperature "
            "sweep (`.dc temp <start> <stop> <step>`) across the specified "
            "temperature range (default -40 to 125 C if unspecified), and report "
            "the MEASURED temperature coefficient in ppm/C in \"measured\" - the "
            "tempco spec is unverifiable from a single-temperature run."
            if topology == "bandgap_reference"
            else ""
        ),
    )
    tb_sch_json_line = (
        '\n     "testbench_sch_file": "<the _tb.sch testbench schematic you actually wrote>",'
        if spec.get("testbench_style") == "xschem"
        else ""
    )

    return f"""Design, build, and verify a {display_name} in sky130 in the CURRENT
WORKING DIRECTORY ONLY. Do not touch any files outside this directory. A
project-local `xschemrc` already exists here sourcing the PDK; use it as-is.

Run label: {label}
PHY Type: {phy_type}
Selected Block: {selected_block if selected_block else display_name}


{topology_clause}

## Spec to build against (use these exact values where given; where a value
## says "not specified", use your own engineering judgment and state the
## assumption you made)
{extra_fields_block}
{_spec_line("Power budget", spec.get("power_mw"), "mW")}
{_spec_line("Area budget", spec.get("area_um2"), "um^2")}
{_spec_line("Supply voltage VDD", vdd_v, "V")}
{_spec_line("Simulation temperature", temp_c, "C (`.temp` in the testbench)")}
{_spec_line("Process corner", corner, "(sky130 tt/ff/ss/sf/fs)")}
{_spec_line("Additional notes from the user", spec.get("notes"))}
{_vdd_headroom_note(vdd_v)}
Use `$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice` with the `{corner}`
corner in any testbench `.lib` include.

## What to actually do (all in this directory)
1. State the topology and sizing/biasing choices you're making, and why,
   briefly (per your usual practice) - including any assumption you made for
   a spec value left unspecified above.
2. Hand-write the xschem schematic (a `.sch` file) per your standard
   authoring workflow, with clearly labeled input/output/supply pins.{_cell_naming_note(spec)}
3. Netlist it headlessly (`xschem -x -q -n -s <name>.sch`) and check the
   resulting connectivity before moving on.
4. Render the schematic to a PNG in this directory via the standard Xvfb
   workflow (hide layers 15/17), then Read the PNG and visually sanity-check
   it (no overlapping text, real junction dots where nets are meant to
   join, pins clearly labeled) - fix and re-render if not.
{tb_step}
6. Run the testbench in ngspice batch mode, capture the log, and pull the
   real simulated numbers out of it - do not estimate or hand-calculate
   these numbers, report exactly what ngspice measured. If it does not
   converge, say so plainly, include the ngspice error output, and still
   complete steps 7-8 with whatever partial information you have.
7. Save the full ngspice run output to a file named `sim.log` in this
   directory.
8. As the LAST thing you do, write a file named exactly `summary.json` in
   this directory (via the Write tool) containing ONLY a single JSON object
   (no markdown fence, no extra text in that file) with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason if status is "failed",
     "topology": "{display_name}",
     "topology_choice": "<the specific internal topology you chose and why,
       one sentence>",
     "corner": "{corner}",
     "vdd_v": {vdd_v},
     "temp_c": {temp_c},
     "target_spec": {target_spec_json},
     "measured": {{<same keys as "target_spec" above, with the real
       ngspice-measured value for each (or null if that particular metric
       isn't practically measurable in this environment/testbench) - e.g.
       for a PLL that's "ref_freq_mhz", "output_freq_ghz",
       "loop_bandwidth_mhz", "phase_margin_deg", "power_mw", "area_um2".
       Add any other metric relevant to this specific block beyond those
       keys too (e.g. "jitter_ps" if measurable) - do not estimate or
       hand-calculate, only report what you actually simulated>}},
     "schematic_png": "<the PNG filename you actually wrote>",
     "schematic_file": "<the .sch filename you actually wrote>",
     "netlist_file": "<the netlisted .spice filename you actually wrote>",
     "testbench_file": "<the _tb.spice testbench netlist ngspice actually ran>",{tb_sch_json_line}
     "sim_log_file": "sim.log",
     "notes": "<any caveats, e.g. non-convergence, an assumption you made for
       an unspecified spec value, anything you could not verify and why>"
   }}
   Write this file even if something failed along the way - in that case set
   status to "failed", fill in "error", and leave any fields you could not
   measure as null. This file is the single source of truth another program
   will read to display results, so it MUST exist and MUST be valid JSON
   parseable on its own, and its "status" field must honestly reflect
   whether this run produced a usable, verified schematic/netlist - do not
   report "success" if the simulation did not converge or the netlist
   connectivity was wrong.

After writing summary.json, give a brief (3-5 sentence) plain-English
summary of what you built and measured as your final reply.
"""


# ---------------------------------------------------------------------------
# Single-deliverable prompts (RunSpec.deliverable != "full"). Each is a
# self-contained prompt for exactly one output; the full-flow prompts above
# are deliberately untouched.
# ---------------------------------------------------------------------------


def _spec_context_block(spec: dict[str, Any]) -> str:
    """The spec-values section shared by the single-deliverable prompts:
    the current mirror's fixed sizing fields, or the schema-driven
    extra-fields lines every other topology uses (same formatting helpers as
    the full prompts, so the two can't drift)."""
    topology = spec.get("topology", "nmos_current_mirror")
    vdd_v = spec["vdd_v"]
    corner = spec["corner"]
    temp_c = spec.get("temp_c", 27)
    if topology == "nmos_current_mirror":
        iref_ua = spec["iref_ua"]
        ratio_out = spec["ratio_out"]
        ratio_ref = spec["ratio_ref"]
        lines = [
            f"- Reference current Iref = {iref_ua} uA",
            f"- Mirror ratio (output:reference) = {ratio_out}:{ratio_ref}",
            f"- Target output current = {round(iref_ua * ratio_out / ratio_ref, 4)} uA",
            f"- Reference device sizing: W = {spec['w_ref_um']} um, L = {spec['l_ref_um']} um "
            f"(mirror device W scales by the ratio, same L)",
        ]
    else:
        lines = format_extra_field_lines(topology, spec.get("extra_fields")) or [
            "- (no structured spec fields defined for this topology - see any notes below)"
        ]
        lines.append(_spec_line("Power budget", spec.get("power_mw"), "mW"))
        lines.append(_spec_line("Area budget", spec.get("area_um2"), "um^2"))
        lines.append(_spec_line("Additional notes from the user", spec.get("notes")))
    lines.append(_spec_line("Supply voltage VDD", vdd_v, "V"))
    lines.append(_spec_line("Simulation temperature", temp_c, "C"))
    lines.append(_spec_line("Process corner", corner, "(sky130 tt/ff/ss/sf/fs)"))
    return "\n".join(lines)


def _chosen_architecture_clause(spec: dict[str, Any]) -> str:
    chosen = spec.get("chosen_architecture")
    if not chosen:
        return ""
    rationale = spec.get("chosen_architecture_rationale")
    rationale_clause = f" Circuit_Researcher's rationale: {rationale}" if rationale else ""
    return (
        f"\nArchitecture already chosen by the user (from a Circuit_Researcher "
        f"comparison): **{chosen}**.{rationale_clause} Build this specific "
        "architecture unless it is clearly infeasible in sky130 at this supply "
        "- if so, say why and pick the closest viable alternative.\n"
    )


def _build_schematic_only_prompt(spec: dict[str, Any]) -> str:
    """Deliverable = schematic_only: design the circuit and produce netlist +
    xschem .sch + rendered PNG. NO verification simulation suite (a quick DC
    operating-point sanity check is allowed), no behavioral models."""
    topology = spec.get("topology", "nmos_current_mirror")
    display_name = topology_display_name(topology, spec.get("custom_topology"))
    label = spec.get("label") or "unlabeled run"
    corner = spec["corner"]
    vdd_v = spec["vdd_v"]
    temp_c = spec.get("temp_c", 27)

    if topology == "nmos_current_mirror" and not spec.get("chosen_architecture"):
        topology_note = (
            "Standard two-transistor NMOS current mirror: diode-connected M1 "
            "carrying Iref from an ideal current source pulled up to VDD, M2 "
            "mirroring to an output pin IOUT, both `sky130_fd_pr__nfet_01v8`, "
            "bulk to GND. Derive M2's W by scaling M1's W by the mirror ratio "
            "(same L)."
        )
    else:
        topology_note = (
            "Use your own domain expertise to choose a sky130-appropriate "
            "internal topology/sizing that can plausibly meet the spec below, "
            "and briefly state and justify that choice before drawing."
        )

    return f"""Design and DRAW a {display_name} in sky130 in the CURRENT WORKING
DIRECTORY ONLY. Do not touch any files outside this directory. A
project-local `xschemrc` already exists here sourcing the PDK; use it as-is.

Run label: {label}

SCOPE - SCHEMATIC-ONLY RUN: this run's deliverables are the xschem schematic,
its netlist, and a rendered PNG. Do NOT build the verification simulation
suite (no AC/tran spec-verification testbenches, no spec-metric
measurements) and do NOT write any behavioral models. The only simulation
allowed is one quick DC operating-point sanity check of the bias points.

## What to build
{topology_note}
{_chosen_architecture_clause(spec)}
## Spec to design against (use these exact values where given; where a value
## says "not specified", use your own engineering judgment and state it)
{_spec_context_block(spec)}
{_vdd_headroom_note(vdd_v)}
## What to actually do (all in this directory)
1. Briefly state the topology and sizing/biasing choices you're making.
2. Hand-write the xschem schematic (a `.sch` file) per your standard
   authoring workflow, with clearly labeled input/output/supply pins.{_cell_naming_note(spec)}
3. Netlist it headlessly (`xschem -x -q -n -s <name>.sch`) and check the
   resulting connectivity before moving on.
4. Render the schematic to a PNG in this directory via the standard Xvfb
   workflow (hide layers 15/17), then Read the PNG and visually sanity-check
   it (no overlapping text, junction dots where nets join, pins labeled) -
   fix and re-render if not.
5. Quick DC sanity check ONLY (skip if a DC operating point is not
   meaningful for this block, and say so): a minimal hand-written `.op`
   testbench at VDD = {vdd_v} V, corner `{corner}`, `.temp {temp_c}`, run in
   ngspice batch mode just to confirm the bias points are sane (devices in
   the intended regions, no floating nodes). Save its output to
   `dc_sanity.log`. Do not iterate on spec performance - that is the full
   flow's job, not this run's.
6. As the LAST thing you do, write a file named exactly `summary.json` in
   this directory (via the Write tool) containing ONLY a single JSON object
   with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason if status is "failed",
     "deliverable": "schematic_only",
     "topology": "{display_name}",
     "topology_choice": "<the internal topology you chose and why, one sentence>",
     "corner": "{corner}",
     "vdd_v": {vdd_v},
     "temp_c": {temp_c},
     "schematic_png": "<the PNG filename you actually wrote>",
     "schematic_file": "<the .sch filename you actually wrote>",
     "netlist_file": "<the netlisted .spice filename you actually wrote>",
     "dc_log_file": "dc_sanity.log" or null if you skipped the DC check,
     "dc_check_note": "<one sentence: what the DC sanity check showed, or why skipped>",
     "notes": "<any caveats or assumptions>"
   }}
   Write this file even if something failed along the way (status "failed",
   fill in "error", null what you don't have). Its "status" must honestly
   reflect whether a usable schematic + netlist + PNG exist.

After writing summary.json, give a brief (2-4 sentence) plain-English
summary of what you drew as your final reply.
"""


def _build_symbol_prompt(spec: dict[str, Any]) -> str:
    """Deliverable = symbol: ONLY an xschem .sym for the block. Cheap, no
    simulations. When run_circuit_builder found a filed library schematic for
    this block it copied it into the run dir and recorded its filename in
    spec['_symbol_source_sch'] (and the cell name in spec['_symbol_cell']);
    the pins come from there. Otherwise the pin list is derived from the
    block's spec/standard interface."""
    topology = spec.get("topology", "nmos_current_mirror")
    display_name = topology_display_name(topology, spec.get("custom_topology"))
    label = spec.get("label") or "unlabeled run"
    cell = spec.get("_symbol_cell") or topology
    source_sch = spec.get("_symbol_source_sch")

    if source_sch:
        pin_source = f"""## Pin source: the filed schematic
The block's filed library schematic has been copied into this directory as
`{source_sch}`. Read it and derive the symbol's pins EXACTLY from its
top-level I/O pins (`ipin`/`opin`/`iopin` instances and their lab= names) -
the symbol must be electrically consistent with that schematic so xschem can
descend from symbol to schematic. Do not invent extra pins."""
    else:
        pin_source = f"""## Pin source: the spec (no filed schematic exists for this block yet)
There is no filed schematic to read pins from. Derive a sensible, standard
pin list for a {display_name} from the spec below (signal ins/outs, clocks
or control words where the block type needs them, plus VDD/GND supply pins),
and record the list you chose in summary.json's "ports".

## Spec context
{_spec_context_block(spec)}"""

    return f"""Create an xschem SYMBOL for a {display_name} in the CURRENT WORKING
DIRECTORY ONLY. Do not touch any files outside this directory. A
project-local `xschemrc` already exists here sourcing the PDK; use it as-is.

Run label: {label}

SCOPE - SYMBOL-ONLY RUN: the single deliverable is `{cell}.sym` (use exactly
that filename). No schematic drawing, no netlisting of a circuit, no SPICE
simulation, no behavioral models.

{pin_source}

## What to actually do (all in this directory)
1. Hand-write `{cell}.sym` per your standard xschem symbol-authoring
   workflow: a clean box outline, pin attributes (`type=...`,
   `format="@name"` style template as appropriate), pins on the standard
   grid with correct `dir=in/out/inout`, pin name labels, and the cell name
   as the symbol's visible label.
2. Verify it cheaply: write a tiny wrapper schematic `symbol_check.sch` that
   instantiates `{cell}.sym` with named nets on every pin, netlist it
   headlessly (`xschem -x -q -n -s symbol_check.sch`), and confirm every pin
   appears in the netlist with the right name/order. Fix the symbol and
   re-check if not.
3. As the LAST thing you do, write a file named exactly `summary.json` in
   this directory (via the Write tool) containing ONLY a single JSON object
   with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason if status is "failed",
     "deliverable": "symbol",
     "topology": "{display_name}",
     "cell": "{cell}",
     "symbol_file": "{cell}.sym",
     "ports": ["<every pin name, in pin order>"],
     "port_source": {"\"filed_schematic\"" if source_sch else "\"spec_derived\""},
     "notes": "<any caveats, e.g. an ambiguous pin you had to judge>"
   }}
   Write this file even if something failed (status "failed", fill "error").

After writing summary.json, give a brief (1-3 sentence) plain-English
summary as your final reply.
"""


def _build_model_only_prompt(spec: dict[str, Any], model_type: str) -> str:
    """Deliverable = veriloga|verilog|rnm: generate JUST that behavioral
    model from the spec - no transistor-level design, no schematic. The
    per-type contract text (deliverable filenames, coding restrictions,
    verification commands, pass tokens, honesty rules) is the SAME
    models_contract section the full flow uses, in standalone framing."""
    topology = spec.get("topology", "nmos_current_mirror")
    display_name = topology_display_name(topology, spec.get("custom_topology"))
    label = spec.get("label") or "unlabeled run"
    corner = spec["corner"]
    vdd_v = spec["vdd_v"]
    temp_c = spec.get("temp_c", 27)
    target_spec_obj = extra_fields_target_dict(topology, spec.get("extra_fields"))
    if topology == "nmos_current_mirror":
        target_spec_obj = {
            "iref_ua": spec.get("iref_ua"),
            "ratio_out": spec.get("ratio_out"),
            "ratio_ref": spec.get("ratio_ref"),
        }
    target_spec_json = _json_literal(target_spec_obj)

    header = f"""Author and verify ONE behavioral model - {MODEL_LABELS[model_type]} -
of a {display_name}, in the CURRENT WORKING DIRECTORY ONLY. Do not touch any
files outside this directory.

Run label: {label}

SCOPE - MODEL-ONLY RUN: this run has NO transistor-level design step. Do not
draw a schematic, do not write a circuit netlist, do not size any
transistors. The only deliverables are the model files, self-checking
testbench, and verification log described below.

## Spec the model must represent (use these exact values where given; where
## a value says "not specified", pick a defensible judgment value and
## document it in the model header and summary notes)
{_spec_context_block(spec)}
"""

    models_section = build_models_prompt_section(topology, [model_type], standalone=True)

    closing = f"""
## summary.json (the LAST file you write, via the Write tool)
A single JSON object (no markdown fence, no extra text in the file) with
exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason if status is "failed",
     "deliverable": "{model_type}",
     "topology": "{display_name}",
     "corner": "{corner}",
     "vdd_v": {vdd_v},
     "temp_c": {temp_c},
     "target_spec": {target_spec_json},
     "notes": "<assumptions made for unspecified spec fields, caveats>"
   }}
   PLUS the "models" key exactly as described in the model section above
   (containing only "{model_type}"). Top-level "status" is "success" only if
   the model was generated AND its verification outcome is honest -
   "verified" claims are cross-checked against the pass token in the sim log
   by another program. A model whose testbench failed means status "failed".
   Write this file even if something failed along the way.

After writing summary.json, give a brief (2-4 sentence) plain-English
summary including the verification outcome as your final reply.
"""
    return header + models_section + closing


ARCH_PASS_TOKEN = "ARCH_TB_PASS"
ARCH_FAIL_TOKEN = "ARCH_TB_FAIL"


def _safe_verilog_id(raw: str) -> str:
    """Block/diagram ids may contain '-' etc.; Verilog identifiers may not."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw or "blk")
    return safe if re.match(r"[A-Za-z_]", safe) else f"b_{safe}"


def arch_deliverable_files(spec: dict[str, Any]) -> dict[str, Any]:
    """Fixed filenames for an arch_model run: one model file per included
    designable block, a top-level wiring module, its self-checking testbench,
    and the verification log. Shared by the prompt builder, evaluate_summary
    and the tests so the contract cannot drift."""
    model_type = spec.get("arch_model_type") or "rnm"
    included = validate_arch_model_request(
        spec.get("architecture"), spec.get("arch_blocks"), model_type
    )
    # Top-module (and library cell) name: the PHY type for a whole-diagram
    # model, the block ids for a small subset - otherwise every subset model
    # of the same PHY files into the SAME cell and silently overwrites the
    # previous one (seen in the first real user-driven runs: a tx-ffe-only
    # and an rx-eq-only model both landing as "ser_des_arch").
    if spec.get("arch_blocks") and len(included) <= 2:
        top = "_".join(_safe_verilog_id(b["id"]) for b in included) + "_arch"
    else:
        top = _safe_verilog_id(spec.get("phy_type") or "phy") + "_arch"
    sfx = "_rnm.sv" if model_type == "rnm" else ".v"
    return {
        "top": top,
        "model_type": model_type,
        "included": included,
        "block_files": {b["id"]: f"{_safe_verilog_id(b['id'])}{sfx}" for b in included},
        "top_file": f"{top}{sfx}",
        "testbench_file": f"{top}_tb{'.sv' if model_type == 'rnm' else '.v'}",
        "sim_log_file": "arch_sim.log",
    }


def _build_arch_model_prompt(spec: dict[str, Any]) -> str:
    """Deliverable = arch_model: ONE composite behavioral model of the block
    diagram (or the user-chosen subset): a model module per included block
    plus a top-level module wiring them per the diagram edges, verified by a
    self-checking end-to-end testbench. No transistor-level design."""
    files = arch_deliverable_files(spec)
    model_type = files["model_type"]
    included = files["included"]
    included_ids = {b["id"] for b in included}
    top = files["top"]
    label = spec.get("label") or "unlabeled run"
    phy_type = spec.get("phy_type") or "phy"
    arch = spec.get("architecture") or {}
    edges = arch.get("edges") or arch.get("connections") or []
    level_label = "RNM (SystemVerilog, real-valued ports)" if model_type == "rnm" else "digital Verilog (event-driven, bit-level)"
    rules = RNM_RULES if model_type == "rnm" else VERILOG_RULES

    block_lines = []
    for b in included:
        guidance = model_guidance(b.get("topology") or "custom", model_type)
        block_lines.append(
            f"- `{b['id']}` ({b.get('label') or b['id']}; topology: {b.get('topology')}), "
            f"model file `{files['block_files'][b['id']]}`, module `{_safe_verilog_id(b['id'])}"
            f"{'_rnm' if model_type == 'rnm' else ''}`.\n"
            f"  What this block's model must capture: {guidance}"
        )
    skipped = [
        f"`{b.get('id')}`" for b in (arch.get("blocks") or [])
        if isinstance(b, dict) and b.get("id") and b["id"] not in included_ids
    ]
    edge_lines = [
        f"- {e['from']} -> {e['to']}"
        for e in edges
        if isinstance(e, dict) and e.get("from") in included_ids and e.get("to") in included_ids
    ] or ["- (no edges between the included blocks - instantiate them side by side)"]

    tools = toolchain_status()
    iverilog_ok = tools["iverilog"]["available"]
    verify_note = "" if iverilog_ok else (
        "\nNOTE: `iverilog`/`vvp` are NOT installed on this machine right now. Still "
        "write every file (respecting every rule above), skip compile/run, and set "
        '"model_status" to "generated_unverified" with status_reason "iverilog not installed".')

    return f"""Author and verify a COMPOSITE behavioral model of a {phy_type} PHY block
diagram at the {level_label} level, in the CURRENT WORKING DIRECTORY ONLY.
Do not touch any files outside this directory.

Run label: {label}

SCOPE - ARCHITECTURE-MODEL RUN: no transistor-level design, no schematics,
no SPICE. The deliverables are one behavioral model module per included
block, a top-level module wiring them together per the diagram edges, a
self-checking testbench, and its verification log.

## Blocks to model (each gets its own module in its own file)
{chr(10).join(block_lines)}
{("Diagram blocks NOT included in this model (pads / not selected by the user): " + ", ".join(skipped) + ".") if skipped else ""}

## Signal-flow edges to wire in the top-level module `{top}` (file `{files['top_file']}`)
{chr(10).join(edge_lines)}
Wiring rules: define ONE consistent inter-block signal convention and
document it in the top module's header comment - for RNM, a `real` data
signal per edge (clock-generating blocks drive an event/bit clock signal to
the blocks they feed); for digital Verilog, a bit/vector per edge. Where a
block model's natural ports don't map 1:1 onto a coarse diagram edge, make a
documented judgment call rather than inventing extra diagram connectivity.
Expose the chain's primary input(s) and output(s) as top-level ports.

## Parameter values
No per-block spec numbers were entered for this run: give every model
parameter a defensible engineering-judgment default for a generic {phy_type}
PHY (document each in the file headers), and keep the blocks' defaults
mutually consistent (e.g. one data rate across the chain).
{_spec_line("Additional notes from the user", spec.get("notes"))}

{rules}

## Verification (run it yourself)
```
iverilog -g2012 -o {top}_tb.vvp {" ".join(sorted(files["block_files"].values()))} {files['top_file']} {files['testbench_file']}
vvp {top}_tb.vvp     # exit 0 AND prints {ARCH_PASS_TOKEN}; save output to {files['sim_log_file']}
```
The testbench `{files['testbench_file']}` must be SELF-CHECKING end to end:
drive a known input pattern into the chain's first block, and check
propagation through the top module (a known-answer check where the chain
allows it; at minimum: the final output responds to the input, no stuck/X
outputs, plus the mandatory watchdog). It prints exactly one of
`{ARCH_PASS_TOKEN}` or `{ARCH_FAIL_TOKEN} <reason>`.{verify_note}

## summary.json (the LAST file you write, via the Write tool)
A single JSON object with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason,
     "deliverable": "arch_model",
     "model_type": "{model_type}",
     "cell": "{top}",
     "top_file": "{files['top_file']}",
     "block_files": {json.dumps(files['block_files'])},
     "testbench_file": "{files['testbench_file']}",
     "sim_log_file": "{files['sim_log_file']}",
     "model_status": "verified" | "generated_unverified" | "failed",
     "status_reason": "<why, when not verified>",
     "blocks_included": {json.dumps(sorted(included_ids))},
     "notes": "<judgment defaults chosen, wiring caveats>"
   }}
   Be honest: "verified" is cross-checked against {ARCH_PASS_TOKEN} in
   {files['sim_log_file']} by another program. A failed compile or testbench
   means model_status "failed" and status "failed".

After writing summary.json, give a brief (2-4 sentence) plain-English
summary including the verification outcome as your final reply.
"""


def _build_arch_stitch_prompt(spec: dict[str, Any]) -> str:
    """Deliverable = arch_stitch: a TOP-LEVEL xschem schematic stitching the
    block diagram together as a design - one instance per included block,
    placed from the filed library cell the user mapped it to (arch_cell_map),
    wired per the diagram edges. Netlist + rendered PNG; no simulation (the
    stitched chain's verification is a full-chain job for a later run)."""
    from libraries import validate_stitch_cell_map  # avoid import cycle at module load

    included = normalize_arch_blocks(spec.get("architecture"), spec.get("arch_blocks"))
    cell_map = validate_stitch_cell_map(included, spec.get("arch_cell_map"))
    included_ids = {b["id"] for b in included}
    arch = spec.get("architecture") or {}
    edges = arch.get("edges") or arch.get("connections") or []
    phy_type = spec.get("phy_type") or "phy"
    top = spec.get("cell") or f"{_safe_verilog_id(phy_type)}_top"
    label = spec.get("label") or "unlabeled run"

    block_lines = []
    # Cells mapped without a symbol view: this run generates their .sym FIRST
    # (from the filed schematic copied into this directory), then
    # instantiates the freshly written local symbol in the top level.
    needs_symbol: dict[str, str] = {}  # cell -> sym filename
    for b in included:
        library, cell, has_symbol = cell_map[b["id"]]
        if has_symbol:
            sym_ref = f"the filed symbol `{library}/{cell}/{cell}.sym`"
        else:
            needs_symbol[cell] = f"{cell}.sym"
            sym_ref = (
                f"the symbol `{cell}.sym` you generate in step 1 below "
                "(it resolves from this directory)"
            )
        block_lines.append(
            f"- `{b['id']}` ({b.get('label') or b['id']}; topology: {b.get('topology')}): "
            f"instantiate {sym_ref} (instance name `x_{_safe_verilog_id(b['id'])}`)"
        )
    symbol_section = ""
    if needs_symbol:
        sym_lines = "\n".join(
            f"- `{cell}.sym` - derive its pins EXACTLY from the top-level I/O pins "
            f"(`ipin`/`opin`/`iopin` instances and their lab= names) of `{cell}.sch`, "
            "which has been copied into this directory. Do not invent extra pins."
            for cell in sorted(needs_symbol)
        )
        symbol_section = f"""
## Symbols to generate FIRST (these cells have no symbol view yet)
{sym_lines}
Author each per your standard xschem symbol workflow (clean box outline, pin
attributes, standard-grid pins with correct dir=, name labels, the cell name
as the visible label). These symbols are also a deliverable of this run -
they get filed back into their block's library cell afterwards.
"""
    skipped = [
        f"`{b.get('id')}`" for b in (arch.get("blocks") or [])
        if isinstance(b, dict) and b.get("id") and b["id"] not in included_ids
    ]
    edge_lines = [
        f"- {e['from']} -> {e['to']}"
        for e in edges
        if isinstance(e, dict) and e.get("from") in included_ids and e.get("to") in included_ids
    ] or ["- (no edges between the included blocks - place them side by side, unconnected)"]

    return f"""Stitch a {phy_type} PHY block diagram together as a TOP-LEVEL xschem
DESIGN schematic in the CURRENT WORKING DIRECTORY ONLY. Do not touch any
files outside this directory. A project-local `xschemrc` already exists here
sourcing the PDK AND putting the tool's design libraries on
XSCHEM_LIBRARY_PATH - the filed symbols below resolve through it as-is.

Run label: {label}

SCOPE - STITCH-ONLY RUN: the deliverables are ONE top-level schematic
`{top}.sch` instantiating already-designed blocks (plus its netlist and a
rendered PNG), and any block symbols that had to be generated along the way.
Do NOT design or modify any block internals, do NOT run any simulation -
the blocks were designed and verified by their own runs.

## Blocks to place (from their filed library cells)
{chr(10).join(block_lines)}
{symbol_section}
{("Diagram blocks NOT included (pads / not selected): " + ", ".join(skipped) + " - represent a pad connection with a labeled top-level pin instead of an instance.") if skipped else ""}

## Signal-flow edges to wire
{chr(10).join(edge_lines)}
Wiring rules: read each instantiated symbol first (its .sym file resolves on
XSCHEM_LIBRARY_PATH) and wire REAL pin names - never invent pins. Connect
each edge with a sensibly named net (signal path names like `rx_in`,
`eq_out`); where a symbol has more pins than the coarse diagram edge implies
(bias, enable, clocks), tie them to sensibly named/labeled nets or top-level
pins and say what you did in "notes". Give every instance shared `VDD`/`GND`
supply rails. Expose the chain's external connections (pad-facing signals,
reference inputs) as labeled top-level ipin/opin pins.
Placement: lay instances out left-to-right in signal-flow order (the edge
list above), clock/bias blocks below the main path, with enough spacing that
wires stay readable.

## What to actually do (all in this directory)
1. {"Generate the missing symbols listed above (pins exactly from their copied .sch files), then read" if needs_symbol else "Read"} each instantiated .sym to collect real pin names/geometry.
2. Hand-write `{top}.sch` per your standard authoring workflow: the
   instances, wires, supply rails, labeled top-level pins, and a short title.
3. Netlist it headlessly (`xschem -x -q -n -s {top}.sch`) and CHECK the
   netlist: one subcircuit call per instance above, edge nets connecting the
   right instances. (Subcircuit DEFINITIONS may be unresolved in this
   netlist if a cell has no schematic view here - that is expected and fine;
   note any such cell in "notes".)
4. Render `{top}.png` via the standard Xvfb workflow (hide layers 15/17),
   Read it and visually sanity-check (no overlapping instances/text, wires
   actually reaching pins) - fix and re-render if not.
5. As the LAST thing you do, write `summary.json` (via the Write tool):
   a single JSON object with exactly these keys:
   {{
     "status": "success" or "failed",
     "error": "" or a short plain-English reason,
     "deliverable": "arch_stitch",
     "cell": "{top}",
     "schematic_file": "{top}.sch",
     "schematic_png": "{top}.png",
     "netlist_file": "{top}.spice",
     "blocks_instantiated": {json.dumps({bid: f"{lib}/{cell}" for bid, (lib, cell, _hs) in cell_map.items()})},
     "symbols_generated": {json.dumps(dict(sorted(needs_symbol.items())))},
     "top_ports": ["<the labeled top-level pins you exposed>"],
     "notes": "<extra-pin tie-offs, unresolved subckt definitions, judgment calls>"
   }}
   "status" is "success" only if the schematic, netlist and PNG all exist
   and the netlist really instantiates every block above.

After writing summary.json, give a brief (2-4 sentence) plain-English
summary as your final reply.
"""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort fallback: pull a fenced ```json ... ``` block out of chat text."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _format_event(line: str) -> str | None:
    """Turn one --output-format stream-json line into a human-readable log
    line (or a few). Returns None only for events not worth showing."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return f"[raw] {line}"

    etype = ev.get("type")
    if etype == "system" and ev.get("subtype") == "init":
        return f"[session] started (model={ev.get('model')}, agent from CLAUDE_AGENT env or --agent flag)"
    if etype == "assistant":
        parts = []
        for block in ev.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(f"[assistant] {block['text']}")
            elif btype == "thinking" and block.get("thinking"):
                snippet = block["thinking"].strip().replace("\n", " ")
                if len(snippet) > 300:
                    snippet = snippet[:300] + "..."
                parts.append(f"[thinking] {snippet}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {}) or {}
                if name == "Bash":
                    detail = inp.get("command", "")
                elif name in ("Write", "Edit"):
                    detail = inp.get("file_path", "")
                elif name == "Read":
                    detail = inp.get("file_path", "")
                else:
                    detail = json.dumps(inp)[:200]
                parts.append(f"[tool] {name}: {detail}")
        return "\n".join(parts) if parts else None
    if etype == "user":
        # Tool results being fed back to the model - useful but verbose;
        # show a short excerpt so the log conveys progress without being a
        # full duplicate of every file/command output.
        for block in ev.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                text = text.strip().replace("\n", " ")
                if len(text) > 250:
                    text = text[:250] + "..."
                return f"[tool result] {text}"
        return None
    if etype == "result":
        return (
            f"[session] finished: is_error={ev.get('is_error')} "
            f"cost=${ev.get('total_cost_usd', 0):.4f} "
            f"duration={ev.get('duration_ms', 0)}ms"
        )
    return None


def _prepare_symbol_inputs(spec: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """For a symbol-only run: locate the block's filed library cell (chosen
    library, matching topology), copy its schematic view into the run dir,
    and return the private spec keys _build_symbol_prompt consumes. When no
    filed schematic exists the symbol is derived from the spec instead
    (_symbol_source_sch = None)."""
    from libraries import find_cell_for_topology, find_library_dir  # avoid import cycle at module load

    topology = spec.get("topology") or "cell"
    library = spec.get("library")
    cell: str | None = None
    source: str | None = None
    if library:
        try:
            # The user may have targeted an existing cell explicitly (the
            # library cell picker); otherwise fall back to whichever cell in
            # the library was filed for this topology.
            cell = spec.get("cell") or find_cell_for_topology(library, topology)
        except ValueError:
            cell = None
        if cell:
            lib_dir = find_library_dir(library)
            cell_dir = (lib_dir / cell) if lib_dir else None
            sch = cell_dir / f"{cell}.sch" if cell_dir else None
            if sch is None or not sch.is_file():
                # Fall back to the first non-testbench .sch view in the cell.
                sch = next(
                    (f for f in sorted(cell_dir.glob("*.sch")) if not f.name.endswith("_tb.sch")),
                    None,
                ) if cell_dir else None
            if sch is not None and sch.is_file():
                shutil.copy2(sch, run_dir / sch.name)
                source = sch.name
    return {"_symbol_cell": cell or topology, "_symbol_source_sch": source}


def _prepare_stitch_inputs(spec: dict[str, Any], run_dir: Path) -> None:
    """For an arch_stitch run: any mapped cell WITHOUT a symbol view gets its
    filed schematic copied into the run dir, so Circuit_Builder can derive
    and author the missing `<cell>.sym` there (the library trees themselves
    are write-denied to it; the backend files the generated symbols back
    afterwards - see libraries.file_generated_symbols)."""
    from libraries import find_library_dir, validate_stitch_cell_map

    included = normalize_arch_blocks(spec.get("architecture"), spec.get("arch_blocks"))
    cell_map = validate_stitch_cell_map(included, spec.get("arch_cell_map"))
    for _bid, (library, cell, has_symbol) in cell_map.items():
        if has_symbol:
            continue
        lib_dir = find_library_dir(library)
        sch = (lib_dir / cell / f"{cell}.sch") if lib_dir else None
        if sch is not None and sch.is_file() and not (run_dir / sch.name).exists():
            shutil.copy2(sch, run_dir / sch.name)


def evaluate_summary(
    spec: dict[str, Any], run_dir: Path, summary: dict[str, Any]
) -> tuple[str, str, dict[str, str]]:
    """Post-run cross-checks of summary.json against what is actually on
    disk, per deliverable mode. Returns (status, reason, artifacts). Split
    out of run_circuit_builder so it is unit-testable without invoking the
    claude CLI.

    - Every claimed artifact file must exist on disk (all modes).
    - full/schematic_only additionally require a rendered schematic PNG.
    - symbol requires the .sym file.
    - model-only modes run the models honesty pass for exactly the requested
      type and fail the RUN if that model failed (unlike full runs, where a
      model problem only downgrades the per-model badge - there the
      transistor-level result stands on its own; here the model IS the run).
    """
    deliverable = spec.get("deliverable") or "full"

    artifacts: dict[str, str] = {}
    missing: list[str] = []
    for key in (
        "schematic_png", "netlist_file", "testbench_file", "testbench_sch_file",
        "sim_log_file", "schematic_file", "symbol_file", "dc_log_file", "top_file",
    ):
        fname = summary.get(key)
        if fname:
            if (run_dir / fname).exists():
                artifacts[key] = fname
            else:
                missing.append(fname)
    # arch_model per-block model files / arch_stitch generated symbols are
    # dicts of filenames, checked the same way.
    for key in ("block_files", "symbols_generated"):
        for fname in (summary.get(key) or {}).values():
            if fname and not (run_dir / fname).exists():
                missing.append(fname)

    status = summary.get("status", "failed")
    reason = summary.get("error", "") or ""
    if status == "success" and missing:
        status = "failed"
        reason = f"summary.json claimed success but artifact(s) missing on disk: {', '.join(missing)}"
    if deliverable in ("full", "schematic_only", "arch_stitch"):
        if status == "success" and "schematic_png" not in artifacts:
            status = "failed"
            reason = reason or "no rendered schematic PNG found"
    elif deliverable == "symbol":
        if status == "success" and "symbol_file" not in artifacts:
            status = "failed"
            reason = reason or "no .sym symbol file found"
    elif deliverable == "arch_model":
        if status == "success" and "top_file" not in artifacts:
            status = "failed"
            reason = reason or "no top-level architecture model file found"
        # A "verified" claim must be backed by the pass token in the sim log
        # (same honesty rule as the per-block model contract).
        if summary.get("model_status") == "verified":
            log_name = summary.get("sim_log_file")
            log_text = ""
            if log_name and (run_dir / log_name).exists():
                try:
                    log_text = (run_dir / log_name).read_text(errors="replace")
                except OSError:
                    log_text = ""
            if ARCH_PASS_TOKEN not in log_text or ARCH_FAIL_TOKEN in log_text:
                summary["model_status"] = "failed"
                summary["status_reason"] = (
                    f"claimed verified but {log_name or 'sim log'} does not contain "
                    f"{ARCH_PASS_TOKEN} (or contains {ARCH_FAIL_TOKEN})"
                )
        if status == "success" and summary.get("model_status") == "failed":
            status = "failed"
            reason = summary.get("status_reason") or "architecture model verification failed"

    # Behavioral-models honesty pass (cycle 2): claimed files must exist, a
    # "verified" claim must be backed by its pass token in the named sim log.
    # For full runs the requested list is the models checkboxes and a model
    # problem is per-model only; for a model-only run it is the deliverable
    # itself and a failed model fails the run.
    if deliverable in MODEL_TYPES:
        model_requested: list[str] | None = [deliverable]
    elif deliverable == "full":
        model_requested = spec.get("models")
    else:
        model_requested = None
    model_problems = check_models_in_summary(summary, run_dir, model_requested)
    if model_problems:
        note = "Behavioral models: " + "; ".join(model_problems)
        summary["notes"] = f"{summary.get('notes') or ''} {note}".strip()
    if deliverable in MODEL_TYPES and status == "success":
        entry = (summary.get("models") or {}).get(deliverable) or {}
        if entry.get("status") == "failed":
            status = "failed"
            reason = entry.get("status_reason") or f"{deliverable} model generation/verification failed"

    return status, reason, artifacts


def run_circuit_builder(
    spec: dict[str, Any],
    run_dir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
) -> CircuitBuilderResult:
    """
    Invoke Circuit_Builder headlessly to build+verify an NMOS current mirror
    against `spec` inside `run_dir` (must already exist and be empty/fresh -
    one directory per run, never reused, so runs can't clobber each other).

    Streams progress into two files in run_dir as the session runs (readable
    by a caller polling for a live log, not just after completion):
      - session.log        human-readable, one line (or few) per event
      - claude_stream.jsonl raw NDJSON events, for debugging

    If `cancel_event` is set while a run is in progress, the subprocess is
    terminated and the result is reported as a (plain, not hidden) failure.

    Returns a CircuitBuilderResult. Never raises for ordinary failure modes
    (non-convergence, Circuit_Builder erroring, timeout, cancellation) -
    those are reported via status="failed" so the caller can surface them
    plainly instead of crashing the API.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_project_xschemrc(run_dir)

    deliverable = spec.get("deliverable") or "full"
    if deliverable == "symbol":
        # Symbol runs derive pins from the block's filed library schematic
        # when one exists: copy it into the run dir (Circuit_Builder's
        # Read/Write scope is the run dir; the library trees are
        # deliberately write-denied and outside its read workspace) and tell
        # the prompt builder about it via private spec keys.
        spec = {**spec, **_prepare_symbol_inputs(spec, run_dir)}
    elif deliverable == "arch_stitch":
        # Copy in the schematics of mapped cells missing a symbol view, so
        # the run can generate those symbols before stitching.
        _prepare_stitch_inputs(spec, run_dir)

    prompt = build_prompt(spec)
    (run_dir / "prompt.txt").write_text(prompt)

    cmd = [CLAUDE_BIN, "--agent", "Circuit_Builder", "--add-dir", str(PDK_ROOT)]
    for tool in ALLOWED_TOOLS:
        cmd += ["--allowedTools", tool]
    for tool in disallowed_tools():
        cmd += ["--disallowedTools", tool]
    cmd += ["--output-format", "stream-json", "--verbose", "-p", prompt]

    human_log_path = run_dir / "session.log"
    raw_log_path = run_dir / "claude_stream.jsonl"

    started = time.time()
    # Prepend the project-local tools/bin (openvaf lives there - installed
    # without sudo) so Circuit_Builder's Bash(openvaf *) calls resolve.
    env = os.environ.copy()
    env["PATH"] = augmented_path(env.get("PATH", ""))
    proc = subprocess.Popen(
        cmd,
        cwd=str(run_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    final_event: dict[str, Any] | None = None
    cancelled = False
    timed_out = False

    with open(human_log_path, "w") as human_f, open(raw_log_path, "w") as raw_f:
        human_f.write(f"[session] launching Circuit_Builder for run in {run_dir}\n")
        human_f.flush()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.terminate()
                break
            if time.time() - started > timeout_s:
                timed_out = True
                proc.terminate()
                break
            ready, _, _ = select.select([proc.stdout], [], [], POLL_INTERVAL_S)
            if not ready:
                if proc.poll() is not None:
                    break  # process exited with nothing left to read
                continue
            line = proc.stdout.readline()
            if line == "":
                break  # EOF
            raw_f.write(line)
            raw_f.flush()
            try:
                ev = json.loads(line)
                if ev.get("type") == "result":
                    final_event = ev
            except json.JSONDecodeError:
                pass
            formatted = _format_event(line)
            if formatted:
                human_f.write(formatted + "\n")
                human_f.flush()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

        if cancelled:
            human_f.write("[session] cancelled by user\n")
        elif timed_out:
            human_f.write(f"[session] timed out after {timeout_s}s\n")

    duration_ms = int((time.time() - started) * 1000)

    if cancelled:
        return CircuitBuilderResult(status="failed", reason="Cancelled by user", duration_ms=duration_ms)
    if timed_out:
        return CircuitBuilderResult(
            status="failed",
            reason=f"Circuit_Builder invocation timed out after {timeout_s}s",
            duration_ms=duration_ms,
        )
    if proc.returncode != 0:
        return CircuitBuilderResult(
            status="failed",
            reason=f"claude CLI exited with code {proc.returncode}",
            duration_ms=duration_ms,
        )
    if final_event is None:
        return CircuitBuilderResult(
            status="failed",
            reason="claude CLI exited without emitting a final result event",
            duration_ms=duration_ms,
        )

    raw_text = final_event.get("result", "") or ""
    session_id = final_event.get("session_id")
    cost_usd = final_event.get("total_cost_usd")

    if final_event.get("is_error"):
        return CircuitBuilderResult(
            status="failed",
            reason="Circuit_Builder session reported an error",
            raw_text=raw_text,
            session_id=session_id,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    # Source of truth: summary.json written by Circuit_Builder itself.
    summary_path = run_dir / "summary.json"
    summary: dict[str, Any] | None = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            summary = None

    if summary is None:
        # Fallback: try to recover a JSON block from the chat reply, but
        # don't pretend this is as reliable as the file.
        summary = _extract_json_object(raw_text)

    if summary is None:
        return CircuitBuilderResult(
            status="failed",
            reason="Circuit_Builder did not produce a parseable summary.json (or JSON in its reply)",
            raw_text=raw_text,
            session_id=session_id,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    # Cross-check summary.json against what's actually on disk (artifact
    # existence, per-deliverable required outputs, models honesty pass) -
    # see evaluate_summary above.
    status, reason, artifacts = evaluate_summary(spec, run_dir, summary)

    return CircuitBuilderResult(
        status=status,
        reason=reason,
        summary=summary,
        raw_text=raw_text,
        session_id=session_id,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        artifacts=artifacts,
    )
