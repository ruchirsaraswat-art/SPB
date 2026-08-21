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
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from models_contract import (
    augmented_path,
    build_models_prompt_section,
    check_models_in_summary,
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
    # Also put the tool's Virtuoso-style design-library trees (libraries/
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
        "# SPB design libraries (Virtuoso-style lib/cell/view directory tree).\n"
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
   authoring workflow, with clearly labeled input/output/supply pins.
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

    # Cross-check claimed artifacts actually exist on disk - don't trust a
    # "success" status if the files it claims to have made aren't there.
    artifacts: dict[str, str] = {}
    missing: list[str] = []
    for key in ("schematic_png", "netlist_file", "testbench_file", "testbench_sch_file", "sim_log_file", "schematic_file"):
        fname = summary.get(key)
        if fname:
            if (run_dir / fname).exists():
                artifacts[key] = fname
            else:
                missing.append(fname)

    status = summary.get("status", "failed")
    reason = summary.get("error", "") or ""
    if status == "success" and missing:
        status = "failed"
        reason = f"summary.json claimed success but artifact(s) missing on disk: {', '.join(missing)}"
    if status == "success" and "schematic_png" not in artifacts:
        status = "failed"
        reason = reason or "no rendered schematic PNG found"

    # Behavioral models (cycle 2): honesty pass over summary.models - claimed
    # files must exist, a "verified" claim must be backed by its pass token
    # in the named sim log. Downgrades are PER-MODEL (mutating summary),
    # deliberately not failing the whole run: the transistor-level result is
    # still valid and the per-model status badge surfaces the model problem
    # plainly in the UI.
    model_problems = check_models_in_summary(summary, run_dir, spec.get("models"))
    if model_problems:
        note = "Behavioral models: " + "; ".join(model_problems)
        summary["notes"] = f"{summary.get('notes') or ''} {note}".strip()

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
