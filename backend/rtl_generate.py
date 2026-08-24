"""
RTL generation of controller blocks / the whole controller (`rtl`
deliverable) - feature spec: audits/2026-08-23-rtl-gen-spec.md, section b.

POST /api/rtl_generate (own endpoint like fw_generate - digital blocks are a
separate namespace and must never enter the Circuit_Builder analog flow) runs
one headless RTL_Coder session (charter: ~/.claude/agents/RTL_Coder.md) that
writes `<block>.v` + `<block>_tb.sv` per included block (plus the stitched
`<phy>_controller_top.v` for scope=controller) into the run directory, runs
iverilog -g2012 + vvp itself, and reports per-block status in
rtl_summary.json.

THE HONESTY CHECK IS SERVER-ENFORCED AND GOES ONE STEP FURTHER THAN THE
MODELS CONTRACT: after the agent reports done, evaluate_rtl_summary() checks
every claimed/required file exists, then RE-RUNS the iverilog compile + vvp
simulation ITSELF for each block and the top (fixed TB seeds make this
deterministic). The agent's claimed status is overwritten by the server
result; a downgrade is recorded in status_reason - exactly the
check_models_in_summary downgrade semantics, made stronger by re-execution.
Run verdict: scope=block - the block must verify; scope=controller -
`success` iff ALL blocks AND the top verify, `partial` (amber) iff the top or
>= 1 block failed but >= 1 block verified (verified blocks still filed),
`failed` otherwise. Per-vvp watchdog: 300 s.

Runs live in <working_dir>/runs/<run_id>/ with deliverable "rtl" so they
appear in GET /api/runs with cost/duration/status like every other run
(Token_Optimizer visibility). Timeouts: 1800 s (block) / 5400 s (controller)
- neither is a light_run.
"""

from __future__ import annotations

import json
import re
import select
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from circuit_builder import _format_event, disallowed_tools
from digital_blocks import ALL_DIGITAL_BLOCK_VALUES, DIGITAL_BLOCK_GROUPS
from interfaces import normalize_interface, validate_interface
from libraries import find_library_dir, validate_library_name
from settings import libraries_root
from spec_docs import (
    VALID_PHY_TYPES,
    _render_one_doc,
    resolve_docs_for_generation,
)

CLAUDE_BIN = "claude"
POLL_INTERVAL_S = 1.0

RTL_BLOCK_TIMEOUT_S = 1800  # comparable to schematic_only
RTL_CONTROLLER_TIMEOUT_S = 5400  # the most expensive run type in the tool
RERUN_COMPILE_TIMEOUT_S = 120
RERUN_VVP_TIMEOUT_S = 300  # per-invocation watchdog on the server re-run

PASS_TOKEN = "RTL_TB_PASS"
FAIL_TOKEN = "RTL_TB_FAIL"

# Catalog option lookup: value -> {label, fields}.
_CATALOG_BY_VALUE: dict[str, dict[str, Any]] = {
    opt["value"]: opt for group in DIGITAL_BLOCK_GROUPS for opt in group["options"]
}

# Narrow allow-list for the codegen session: file authoring in cwd (the run
# dir - relative "(**)" glob, same hard-learned scoping rule as
# circuit_builder.py), iverilog/vvp/tee and read-only helpers.
ALLOWED_TOOLS = [
    "Write(**)",
    "Edit(**)",
    "Read",
    "Glob",
    "Grep",
    "Bash(iverilog *)",
    "Bash(vvp *)",
    "Bash(tee *)",
    "Bash(ls *)",
    "Bash(ls)",
    "Bash(cat *)",
    "Bash(grep *)",
    "Bash(pwd)",
    "WebFetch",
]

# ---------------------------------------------------------------------------
# Block classes and their concrete self-checking TB criteria (spec section b,
# quoted verbatim into the prompt contract - the tables ARE the contract).

BLOCK_CLASS: dict[str, str] = {
    "elastic_fifo": "fifo",
    "word_aligner": "aligner",
    "lane_deskew": "aligner",
    "csr_regfile": "csr",
    "link_training_fsm": "fsm",
    "power_state_fsm": "fsm",
    "d2d_link_state_fsm": "fsm",
    "zq_cal_fsm": "fsm",
    "ddr_training_engine": "fsm",
    "cal_engine": "fsm",
    "eye_monitor_ctrl": "fsm",
    "line_codec_8b10b": "codec",
    "scrambler_descrambler": "codec",
    "crc_retry_engine": "codec",
    "gearbox": "codec",
    "prbs_gen_checker": "codec",
    "dfi_command_path": "dfi",
    "dfi_write_path": "dfi",
    "dfi_read_path": "dfi",
    # protocol_engine / d2d_sideband_ctrl / lane_repair_mux: "generic"
}

TB_CRITERIA: dict[str, str] = {
    "fifo": """**FIFO class** (numbers reference the block's own catalog field values):

| check | criterion |
|---|---|
| fill_to_full | from empty, write `depth_words` words with reads held off: `full` asserts on exactly the `depth_words`-th write, not before |
| overflow_guard | 8 further write attempts while full: no pointer movement, subsequent readout of all `depth_words` words matches the reference queue exactly |
| drain_to_empty | read all words: `empty` asserts exactly after the last; 8 read attempts while empty do not advance the read pointer |
| pointer_wrap | streaming push+pop for >= 4 x `depth_words` words at matched rate; every output word compared to the reference queue, 0 mismatches |
| ppm_stress | writer clock offset from reader by the `ppm_tolerance` field value (implement as period difference in the TB) for >= 10,000 words: no overflow/underflow; if `skip_symbol` is set, verify skip insert/delete events occur and data (skips excluded) still matches |
| flags | almost_full/almost_empty (if implemented) assert at their parameterized thresholds, checked at threshold-1 / threshold / threshold+1 occupancy |""",
    "aligner": """**Aligner class**:

| check | criterion |
|---|---|
| lock_all_offsets | for EVERY bit offset 0..`parallel_width_bits`-1: stream words containing `align_pattern` at that offset; lock asserts within `lock_threshold_patterns` + `align_latency_cycles` + 8 word clocks |
| no_false_lock | 1,000 pattern-free random words (TB masks out accidental pattern matches or regenerates): lock never asserts |
| passthrough | after lock, 1,000 random words pass through bit-exact (offset-compensated reference) |
| realign | mid-stream, shift the input phase by a random nonzero offset: lock deasserts within the design's loss threshold and re-asserts at the new offset within the lock_all_offsets bound |
| deskew (lane_deskew only) | lane-to-lane skews of 0, `deskew_range_ui`/2 and `deskew_range_ui` UI: outputs word-aligned across all `num_lanes`; skew of `deskew_range_ui`+1 reported as out-of-range status, not silently wrong data |""",
    "csr": """**CSR class**:

| check | criterion |
|---|---|
| reset_values | after reset, every register reads its documented reset value |
| rw_fields | every RW field: write 0xA5A5A5A5-pattern then 0x5A5A5A5A-pattern (masked to field width), read back exact both times; neighboring fields in the same register unchanged |
| ro_fields | every RO field: bus write is ignored (readback unchanged); value tracks the hardware input port within 1 bus-read of a port change |
| w1c_fields | every W1C field: hw event sets bit; read 1; write 1 clears; write 0 leaves set; a hw event in the same cycle as the clearing write wins (bit stays set) - or the documented alternative, checked either way |
| decode | read of >= 4 unmapped addresses returns 0 (or the `bus_protocol` error response) with zero register side effects |
| protocol | >= 20 back-to-back bus transfers with 0 and with random 1-3 cycle wait states: all complete per protocol (APB: PREADY semantics) |""",
    "fsm": """**FSM class**:

| check | criterion |
|---|---|
| state_coverage | TB visits every state (count == `num_states` / documented state list); TB tallies distinct states seen on the state output port and fails if any missing |
| nominal_sequence | with status inputs (pll_locked, cdr_locked, comparator, ...) asserted after plausible delays, FSM reaches ACTIVE/DONE; enable outputs assert in exactly the interface power_sequence step order, one step at a time |
| timeout_paths | for EVERY wait-on-status step: withhold that status (all others normal); FSM enters its ERROR/retry state within timeout + 2 cycles (timeouts overridden to small values via the mandated parameters, e.g. 32 cycles) |
| retry_policy | where the sequence documents "retry N then ERROR": exactly N retries observed, then ERROR, no more |
| algorithm (cal/eye/training engines) | close the loop against a TB-side behavioral plant (e.g. comparator = sign(trim - hidden target)): engine converges to the hidden target within +/-1 LSB in <= the documented step bound (binary search: `trim_resolution_bits` + 2 iterations; linear sweep: 2^bits steps); repeated for 5 randomized hidden targets incl. both endpoints |""",
    "codec": """**Codec class** (apply the rows matching the block's function):

| check | criterion |
|---|---|
| roundtrip_8b10b | all 256 data bytes x both starting disparities (512 cases) + all 12 K-codes encode->decode bit-exact; running disparity stays in {-1,+1} after every symbol |
| error_detect_8b10b | >= 20 known-invalid 10b codes flag code error; >= 20 disparity-violating (legal-code, wrong-RD) symbols flag disparity error; `disparity_error_action` behavior verified; pipeline latency equals `latency_cycles` |
| roundtrip_scrambler | 10,000 random words scramble->descramble == input; self-sync mode: one injected bit error produces exactly (1 + number of feedback taps) output errors then clean recovery; additive mode: wrong seed detected / resync per design |
| crc_retry | 1,000 clean flits accepted; single-bit corruption at 64 random (flit, bit) positions: every one detected, NAK/retry issued, replay from the retry buffer delivers the original flit in order; sustained full-rate traffic with round-trip delay such that exactly `retry_buffer_flits` are outstanding: no stall, no overwrite |
| gearbox_ratio | stream >= 100 x LCM(`input_width_bits`, `output_width_bits`)/`input_width_bits` input words: output bitstream is a bit-exact reassembly, wrap boundaries included |
| prbs | generator->checker: lock, then 0 errors over 100,000 bits; 10 injected single-bit errors counted as exactly 10; counter saturates (does not wrap) at 2^`error_counter_bits`-1 |""",
    "dfi": """**DFI path blocks**: treated as FIFO + gearbox composites (apply the FIFO
and gearbox_ratio criteria to the buffering/width-conversion inside) plus one
timing check each - command slots per `dfi_ratio` land in the correct phase;
`tphy_wrlat_cycles` / `trddata_en_cycles` measured in the TB equal the field
value exactly.""",
    "generic": """**Generic block** (no fixed criteria table for this topology): derive
named checks from the block's documented function and catalog fields - at
minimum a data-path roundtrip/bit-exactness check against a TB-side reference
model and one error/corner-behavior check. Name every check in the `checks`
list.""",
}

CONTROLLER_TOP_TB_CRITERIA = """**Controller-top TB** (an integration smoke, NOT a re-run of the block TBs):

| check | criterion |
|---|---|
| bringup | drive the interface reset/power sequence stimulus (assert status inputs per the sequence rows, timeouts parameter-shrunk): controller reaches ACTIVE; enable outputs observed in sequence order |
| csr_access | via the host bus: read >= 3 ID/status registers, write+readback >= 3 control registers through the real bus ports |
| rx_datapath | inject >= 1,000 words of correctly encoded data (e.g. 8b/10b-coded PRBS with the align pattern) at the AFE RX ports, with the word clock offset by the interface's documented ppm: data emerges at the core ports bit-exact after decode, 0 errors |
| tx_datapath | drive >= 1,000 core-side words: AFE TX port stream decodes back to the input bit-exact |
| timeout_smoke | withhold ONE status input (e.g. cdr_locked): controller reaches its ERROR-visible CSR state |
| port_conformance | TB instantiates the top using EVERY interface-table port by name (a compile-time check that the port contract is verbatim); TB fails if any port is missing/miswidthed |"""


def rtl_timeout_for(scope: str) -> int:
    return RTL_CONTROLLER_TIMEOUT_S if scope == "controller" else RTL_BLOCK_TIMEOUT_S


def module_name(block_id: str) -> str:
    """<block> = diagram block id, '-' mapped to '_' (any other
    non-identifier char also '_' as safety). Module name == file stem."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", str(block_id).replace("-", "_"))
    return stem if re.match(r"[A-Za-z_]", stem) else f"b_{stem}"


def top_module_name(phy_type: str) -> str:
    return f"{phy_type.replace('-', '_')}_controller_top"


def _designable_blocks(architecture: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in architecture.get("blocks", []) if b.get("topology")]


def deliverable_files_for(scope: str, phy_type: str, included: list[dict[str, Any]]) -> list[str]:
    """The fixed deliverable filename set (spec table). Shared by the prompt,
    the honesty check and the tests so the contract can't drift."""
    files: list[str] = []
    for b in included:
        m = module_name(b["id"])
        files += [f"{m}.v", f"{m}_tb.sv", f"{m}_sim.log"]
    if scope == "controller":
        t = top_module_name(phy_type)
        files += [f"{t}.v", f"{t}_tb.sv", f"{t}_sim.log"]
    files.append("rtl_summary.json")
    return files


# ---------------------------------------------------------------------------
# Request validation (422, all problems listed at once)


def validate_rtl_request(
    scope: str,
    phy_type: str,
    architecture: dict[str, Any],
    block_id: str | None,
    block_fields: dict[str, Any] | None,
    iface: dict[str, Any],
    library: str | None,
    cell: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (included_blocks, interface_warnings); raises ValueError
    listing every problem at once (deliverable-modes style)."""
    problems: list[str] = []

    if phy_type not in VALID_PHY_TYPES:
        problems.append(
            f"unknown phy_type {phy_type!r} - must be one of: {', '.join(VALID_PHY_TYPES)}"
        )
    if scope not in ("block", "controller"):
        problems.append(f"scope must be 'block' or 'controller' (got {scope!r})")

    blocks = {b.get("id"): b for b in architecture.get("blocks", [])}
    designable = _designable_blocks(architecture)

    included: list[dict[str, Any]] = []
    if scope == "block":
        if not block_id:
            problems.append("scope='block' requires block_id")
        elif block_id not in blocks:
            problems.append(f"unknown block_id {block_id!r} - not in the diagram")
        elif not blocks[block_id].get("topology"):
            problems.append(
                f"block {block_id!r} is a boundary anchor / non-designable node "
                "(topology null) - it has no RTL to generate"
            )
        else:
            included = [blocks[block_id]]
    elif scope == "controller":
        included = designable
        if not included:
            problems.append("scope='controller' but the diagram has zero designable blocks")

    # Topology must be in the digital catalog.
    for b in included:
        if b["topology"] not in ALL_DIGITAL_BLOCK_VALUES:
            problems.append(
                f"block {b['id']!r}: topology {b['topology']!r} is not in the digital catalog"
            )

    # Required numeric fields: catalog fields with `min` defined and no value.
    # Text fields may be empty and default per catalog help text.
    fields_by_block = block_fields or {}
    for b in included:
        opt = _CATALOG_BY_VALUE.get(b["topology"])
        if opt is None:
            continue
        values = fields_by_block.get(b["id"])
        required = [f["name"] for f in opt.get("fields", []) if "min" in f]
        if values is None and required:
            problems.append(
                f"block {b['id']!r} ({b['topology']}): no block_fields entry - required "
                f"numeric fields: {', '.join(required)}"
            )
            continue
        for fname in required:
            v = (values or {}).get(fname)
            if v is None or v == "":
                problems.append(
                    f"block {b['id']!r} ({b['topology']}): required field '{fname}' has no value"
                )
            else:
                try:
                    float(v)
                except (TypeError, ValueError):
                    problems.append(
                        f"block {b['id']!r} ({b['topology']}): field '{fname}' must be numeric "
                        f"(got {v!r})"
                    )

    # Interface: hard errors block; warnings pass through into the prompt.
    if_errors, if_warnings = validate_interface(iface)
    for e in if_errors:
        problems.append(f"interface: {e}")

    if not library:
        problems.append("library is required (the filing destination)")
    else:
        try:
            validate_library_name(library)
        except ValueError as exc:
            problems.append(str(exc))
    if cell:
        try:
            validate_library_name(cell)
        except ValueError as exc:
            problems.append(str(exc))

    if problems:
        raise ValueError("rtl_generate problems:\n" + "\n".join(f"- {p}" for p in problems))
    return included, if_warnings


# ---------------------------------------------------------------------------
# Prompt


def _block_fields_lines(block: dict[str, Any], values: dict[str, Any] | None) -> str:
    opt = _CATALOG_BY_VALUE.get(block["topology"], {})
    lines = []
    for f in opt.get("fields", []):
        v = (values or {}).get(f["name"])
        if v is None or v == "":
            lines.append(
                f"  - {f['name']} ({f.get('unit') or 'text'}): NOT SET - assume a sensible "
                f"default per the catalog hint and STATE the assumed value in the module "
                f"header comment. Hint: {f.get('help', '')}"
            )
        else:
            lines.append(f"  - {f['name']} ({f.get('unit') or 'text'}): {v}")
    return "\n".join(lines) or "  (no catalog fields)"


def build_spec_docs_generation_block(
    resolved_docs: list[dict[str, Any]], no_spec: bool
) -> str:
    """Consumption mode 2 (generation): metadata block PLUS the absolute file
    path and the selective-Read instruction; or the first-principles statement
    when the answer is no_spec / nothing is attached."""
    if no_spec or not resolved_docs:
        return (
            "## Specification documents\n"
            "No specification document is on file for this controller: requirements come "
            "solely from the catalog fields + interface definition, first-principles.\n"
        )
    lines = ["## Specification documents (requirements source)"]
    for meta in resolved_docs:
        lines.append(_render_one_doc(meta))
        source = meta.get("source") or {}
        if meta.get("abs_path"):
            if meta.get("unreadable_by_agent"):
                lines.append(
                    f"  file: {meta['abs_path']} (format not readable by you - treat as "
                    "citation-only context)"
                )
            else:
                lines.append(f"  file (absolute path): {meta['abs_path']}")
        elif source.get("type") == "url":
            lines.append(f"  url: {source.get('url')}")
        elif source.get("type") == "reference":
            lines.append(f"  reference (citation only, no file on hand): {source.get('citation')}")
    lines.append(
        "\nRead ONLY the relevant_sections locators that apply to the blocks you are "
        "generating (PDF `pages` ranges, max 20 pages per Read), plus at most 10 "
        "additional pages you judge necessary (table of contents lookup allowed). "
        "Never read the whole document. Cite section numbers in RTL comments for every "
        "non-obvious requirement taken from the document. If a needed section is not "
        "listed and you cannot locate it within the extra-page budget, report the gap "
        "as a finding instead of guessing. URL sources: you may WebFetch the URL with "
        "the same selectivity; reference-type sources are context-only (cite, don't read)."
    )
    return "\n".join(lines) + "\n"


def build_rtl_prompt(
    scope: str,
    phy_type: str,
    architecture: dict[str, Any],
    included: list[dict[str, Any]],
    block_fields: dict[str, Any] | None,
    iface: dict[str, Any],
    if_warnings: list[str],
    resolved_docs: list[dict[str, Any]],
    no_spec: bool,
) -> str:
    files = deliverable_files_for(scope, phy_type, included)
    top = top_module_name(phy_type)

    # Per-block spec: module name, topology, catalog field values, class.
    block_specs = []
    classes_needed: list[str] = []
    for b in included:
        cls = BLOCK_CLASS.get(b["topology"], "generic")
        if cls not in classes_needed:
            classes_needed.append(cls)
        block_specs.append(
            f"### Block `{b['id']}` -> module/files `{module_name(b['id'])}`\n"
            f"- topology: {b['topology']} "
            f"({_CATALOG_BY_VALUE.get(b['topology'], {}).get('label', '')})\n"
            f"- label: {b.get('label') or b.get('short') or b['id']}\n"
            f"- TB criteria class: {cls}\n"
            f"- catalog field values (these are the RTL parameters - parameterize, never hard-code):\n"
            f"{_block_fields_lines(b, (block_fields or {}).get(b['id']))}"
        )

    criteria = "\n\n".join(TB_CRITERIA[c] for c in classes_needed)
    if scope == "controller":
        criteria += "\n\n" + CONTROLLER_TOP_TB_CRITERIA

    warnings_block = (
        "## Interface soft warnings (non-blocking, for your awareness)\n"
        + "\n".join(f"- {w}" for w in if_warnings)
        + "\n"
        if if_warnings
        else ""
    )

    stitch_block = ""
    if scope == "controller":
        stitch_block = f"""## Whole-controller stitching rules
- Top module `{top}`. AFE-boundary ports come VERBATIM from the interface
  definition's signal table: direction a2d -> input, d2a -> output, ext
  clocks/resets -> input, exact names, `[width-1:0]` for width > 1. Every
  interface signal must appear as a port; a signal no block consumes is
  still a port, tied off with a `// finding:` comment and listed in
  `findings`.
- Core-side and host-side iface anchor nodes map to top-level port groups
  named per the relevant block fields (CSR `bus_protocol` APB ->
  psel/penable/pwrite/paddr[A-1:0]/pwdata[D-1:0]/prdata[D-1:0]/pready;
  protocol engine `flow_control` valid/ready -> core_rx_data/valid/ready,
  core_tx_data/valid/ready; DFI blocks -> dfi_* names from the interface
  default).
- Internal wiring follows the digital diagram edges. An edge between two
  blocks whose data contract is not derivable (no shared signal in the
  interface, no obvious datapath width match) is a reported finding plus a
  best-effort wire ONLY if widths match exactly; otherwise left unconnected
  and reported (the finding + a top TB that still passes its listed checks
  is acceptable).
- Clock domains per the interface `clock_domains` table; each module gets
  the clock its diagram position implies (datapath blocks up to the elastic
  FIFO on the AFE word clock, FIFO read side onward on the core clock,
  CSR/FSMs on the host/ref clock).
"""

    scope_line = (
        f"ONE controller block: `{included[0]['id']}`"
        if scope == "block"
        else f"the WHOLE controller: all {len(included)} designable blocks of the diagram "
        f"plus the stitched top `{top}`"
    )

    return f"""Generate and verify synthesizable RTL for {scope_line} of the {phy_type} PHY's
digital/controller side, in the CURRENT WORKING DIRECTORY ONLY (the tool's
run directory). Do not touch any files outside this directory.

{build_spec_docs_generation_block(resolved_docs, no_spec)}
## Synthesizable-subset rules (your charter restated - the charter is the source of truth)
- Verilog-2005 style RTL: `always @(posedge clk)`, synchronous resets unless
  the interface definition marks the reset async (e.g. rstn_por async
  assert / sync release - then the recognized async-assert pattern,
  commented), no `#` delays or initial-driven logic in DUT code, no latches
  unless explicitly intended and commented, no `$display` in DUT.
- SystemVerilog only in testbenches (`-g2012` covers both).
- CDC crossings use recognized synchronizer patterns (2ff for levels, async
  FIFO with gray-coded pointers for buses, pulse stretch/toggle-sync for
  events) and each is commented `// CDC:` with the from/to domains - these
  must agree with the interface definition's cdc_points table; a crossing in
  the RTL with no cdc_points entry is a finding.
- Widths/depths/timeouts parameterized from catalog fields
  (`parameter DEPTH_WORDS = 16` etc.), never hard-coded. ALL timeout and
  settle counters must be parameters so testbenches can shrink them
  (mandated - the FSM TB criteria depend on it).
- Requirements precedence: spec document (cited) > interface definition >
  catalog fields > agent judgment (documented in a header comment).
  Conflicts and unwireable edges are reported as `findings`, never silently
  improvised around.

## AFE<->Controller interface definition (the port contract at the boundary)
```json
{json.dumps(iface, indent=2)}
```
{warnings_block}
## Digital architecture diagram (edges are the stitch netlist / context)
```json
{json.dumps({"blocks": [{"id": b.get("id"), "label": b.get("label"), "topology": b.get("topology"), "kind": b.get("kind")} for b in architecture.get("blocks", [])], "edges": architecture.get("edges", [])}, indent=2)}
```

## Blocks to generate
{chr(10).join(block_specs)}

{stitch_block}## Self-checking testbench requirements
Universal (every TB, block and top):
- Ends by printing exactly one token: `{PASS_TOKEN}` or `{FAIL_TOKEN} <reason>`
  then `$finish`. Any `$fatal`/assertion path must print the fail token first.
- Watchdog: an independent `initial` block that after `WATCHDOG_CYCLES`
  (>= 10x expected test length, computed from the test's own loop counts)
  prints `{FAIL_TOKEN} watchdog_timeout` and finishes - a hung DUT must never
  stall vvp forever.
- Self-checking against a reference model/queue in the TB - zero reliance on
  waveform inspection. Each named check below appears in the `checks` list of
  rtl_summary.json.
- Randomized stimulus uses a FIXED seed (the tool re-runs your TB and the
  result must be reproducible).

Per-class criteria for the blocks in this run (these named checks are the
contract - implement each and list each in `checks`):

{criteria}

## Verification protocol (run it YOURSELF before reporting done)
For each block, and then the top:
```
iverilog -g2012 -o <block>_tb.vvp <block>.v <block>_tb.sv        # (top: plus all block .v files)
vvp <block>_tb.vvp | tee <block>_sim.log                          # exit 0 AND {PASS_TOKEN}, no {FAIL_TOKEN}
```

## Deliverables (exact filenames, all in the current directory)
{chr(10).join('- ' + f for f in files)}

`rtl_summary.json` is the machine-checked claim:
```json
{{
  "scope": "{scope}",
  "blocks": [
    {{ "block": "<module name>", "topology": "<catalog value>",
      "files": {{ "rtl": "<module>.v", "tb": "<module>_tb.sv", "sim_log": "<module>_sim.log" }},
      "status": "verified" | "failed",
      "status_reason": "",
      "checks": [ {{"name": "<check from the class table>", "result": "pass" | "fail"}} ],
      "spec_citations": ["<doc section numbers actually used, e.g. JESD209-4B 7.2>"] }}
  ],{'''
  "top": { "module": "''' + top + '''", "files": {"rtl": "...", "tb": "...", "sim_log": "..."}, "status": "...", "status_reason": "", "checks": [...] },''' if scope == "controller" else ""}
  "findings": [ "<contract inconsistencies, unwireable edges, spec gaps - verbatim>" ]
}}
```

Report per-block `status` HONESTLY - another program re-runs the iverilog
compile and vvp simulation on the files and pass tokens on disk and will
downgrade a false verified to failed.

Finish with a brief (2-4 sentence) plain-English summary of what you
generated and the verification outcome as your final reply.
"""


# ---------------------------------------------------------------------------
# Agent invocation


def run_rtl_coder(
    prompt: str,
    run_dir: Path,
    timeout_s: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """One headless RTL_Coder codegen session, cwd = the run dir (so the
    relative Write(**) scope is exactly that tree). Streams session.log /
    claude_stream.jsonl into run_dir. Returns {ok, reason, cost_usd,
    raw_text}. Module-level and monkeypatchable so the endpoint tests can
    stub the agent and still exercise the real honesty check."""
    cmd = [CLAUDE_BIN, "--agent", "RTL_Coder"]
    for tool in ALLOWED_TOOLS:
        cmd += ["--allowedTools", tool]
    for tool in disallowed_tools():
        cmd += ["--disallowedTools", tool]
    cmd += ["--output-format", "stream-json", "--verbose", "-p", prompt]

    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(run_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return {"ok": False, "reason": f"`{CLAUDE_BIN}` CLI not found on PATH",
                "cost_usd": None, "raw_text": ""}

    final_event: dict[str, Any] | None = None
    timed_out = False
    cancelled = False
    with open(run_dir / "session.log", "w") as human_f, open(
        run_dir / "claude_stream.jsonl", "w"
    ) as raw_f:
        human_f.write(f"[session] launching RTL_Coder codegen in {run_dir}\n")
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
                    break
                continue
            line = proc.stdout.readline()
            if line == "":
                break
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
        return {"ok": False, "reason": "cancelled by user", "cost_usd": None, "raw_text": ""}
    if timed_out:
        return {"ok": False, "reason": f"RTL_Coder timed out after {timeout_s}s",
                "cost_usd": None, "raw_text": ""}
    if proc.returncode != 0:
        return {"ok": False, "reason": f"claude CLI exited with code {proc.returncode}",
                "cost_usd": None, "raw_text": ""}
    if final_event is None:
        return {"ok": False, "reason": "claude CLI exited without a final result event",
                "cost_usd": None, "raw_text": ""}
    cost = final_event.get("total_cost_usd")
    raw_text = final_event.get("result") or ""
    if final_event.get("is_error"):
        return {"ok": False, "reason": "RTL_Coder session reported an error",
                "cost_usd": cost, "raw_text": raw_text}
    return {"ok": True, "reason": "", "cost_usd": cost, "raw_text": raw_text}


# ---------------------------------------------------------------------------
# Server-enforced honesty check: evaluate_rtl_summary + full re-execution


def _rerun_one(
    run_dir: Path, mod: str, extra_sources: list[str] | None = None
) -> tuple[bool, str]:
    """Server-side re-execution of one module's verification: a FRESH
    iverilog -g2012 compile (fresh output name) + vvp with the 300 s
    watchdog. Pass iff both exit 0 AND the captured vvp log contains the
    literal pass token and no fail token."""
    vvp_out = f"{mod}_tb.server.vvp"
    sources = [f"{mod}.v"] + (extra_sources or []) + [f"{mod}_tb.sv"]
    compile_cmd = ["iverilog", "-g2012", "-o", vvp_out] + sources
    try:
        comp = subprocess.run(
            compile_cmd, cwd=str(run_dir), capture_output=True, text=True,
            timeout=RERUN_COMPILE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"server re-run compile error: {exc}"
    if comp.returncode != 0:
        (run_dir / f"{mod}_sim.server.log").write_text(comp.stdout + comp.stderr)
        return False, f"server re-run: iverilog compile failed (exit {comp.returncode})"
    try:
        sim = subprocess.run(
            ["vvp", vvp_out], cwd=str(run_dir), capture_output=True, text=True,
            timeout=RERUN_VVP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        (run_dir / f"{mod}_sim.server.log").write_text("(killed by server watchdog)")
        return False, "server re-run watchdog (vvp exceeded 300 s)"
    except OSError as exc:
        return False, f"server re-run vvp error: {exc}"
    log = sim.stdout + sim.stderr
    (run_dir / f"{mod}_sim.server.log").write_text(log)
    if sim.returncode != 0:
        return False, f"server re-run: vvp exited {sim.returncode}"
    if FAIL_TOKEN in log:
        reason = next((l for l in log.splitlines() if FAIL_TOKEN in l), FAIL_TOKEN)
        return False, f"server re-run: {reason.strip()}"
    if PASS_TOKEN not in log:
        return False, f"server re-run: no {PASS_TOKEN} token in the simulation output"
    return True, ""


def evaluate_rtl_summary(
    run_dir: Path,
    scope: str,
    phy_type: str,
    included: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    """The pass/fail of the RUN. Returns (verdict, reason, summary) where
    verdict is success|partial|failed and summary is rtl_summary.json with
    every status OVERWRITTEN by the server re-run result (downgrades recorded
    in status_reason, check_models_in_summary semantics)."""
    expected = deliverable_files_for(scope, phy_type, included)
    summary_path = run_dir / "rtl_summary.json"
    if not summary_path.is_file():
        return "failed", "RTL_Coder finished but rtl_summary.json is missing", {}
    try:
        summary = json.loads(summary_path.read_text())
    except json.JSONDecodeError as exc:
        return "failed", f"rtl_summary.json is not valid JSON: {exc}", {}

    # 1. Every deliverable filename exists on disk and is claimed.
    missing_on_disk = [f for f in expected if not (run_dir / f).is_file()]
    claimed: set[str] = {"rtl_summary.json"}
    for entry in summary.get("blocks") or []:
        claimed.update((entry.get("files") or {}).values())
    if isinstance(summary.get("top"), dict):
        claimed.update((summary["top"].get("files") or {}).values())
    missing_claims = [f for f in expected if f not in claimed]
    missing_claimed_files = [
        f for f in sorted(claimed) if f and not (run_dir / f).is_file()
    ]
    if missing_on_disk or missing_claims or missing_claimed_files:
        parts = []
        if missing_on_disk:
            parts.append("deliverables missing on disk: " + ", ".join(missing_on_disk))
        if missing_claims:
            parts.append("deliverables not claimed in rtl_summary.json: " + ", ".join(missing_claims))
        if missing_claimed_files:
            parts.append("claimed files missing on disk: " + ", ".join(missing_claimed_files))
        return "failed", "; ".join(parts), summary

    # 2. Re-run every block, then the top. Server result overwrites the claim.
    entries_by_module = {e.get("block"): e for e in summary.get("blocks") or []}
    block_modules = [module_name(b["id"]) for b in included]
    verified_blocks = 0
    for b in included:
        mod = module_name(b["id"])
        entry = entries_by_module.get(mod)
        if entry is None:
            entry = {"block": mod, "topology": b["topology"], "files": {
                "rtl": f"{mod}.v", "tb": f"{mod}_tb.sv", "sim_log": f"{mod}_sim.log"},
                "checks": [], "spec_citations": []}
            summary.setdefault("blocks", []).append(entry)
        claimed_status = entry.get("status")
        ok, why = _rerun_one(run_dir, mod)
        if ok:
            entry["status"] = "verified"
            if claimed_status not in (None, "verified"):
                entry["status_reason"] = (
                    f"claimed {claimed_status}; server re-run passed"
                )
            verified_blocks += 1
        else:
            entry["status"] = "failed"
            if claimed_status == "verified":
                entry["status_reason"] = f"claimed verified; server re-run failed: {why}"
            else:
                entry["status_reason"] = (entry.get("status_reason") or why or "").strip() or why

    top_ok = True
    if scope == "controller":
        top = top_module_name(phy_type)
        top_entry = summary.get("top") if isinstance(summary.get("top"), dict) else {}
        claimed_status = top_entry.get("status")
        ok, why = _rerun_one(run_dir, top, extra_sources=[f"{m}.v" for m in block_modules])
        top_ok = ok
        top_entry.setdefault("module", top)
        top_entry.setdefault("files", {
            "rtl": f"{top}.v", "tb": f"{top}_tb.sv", "sim_log": f"{top}_sim.log"})
        if ok:
            top_entry["status"] = "verified"
        else:
            top_entry["status"] = "failed"
            if claimed_status == "verified":
                top_entry["status_reason"] = f"claimed verified; server re-run failed: {why}"
            else:
                top_entry["status_reason"] = (top_entry.get("status_reason") or why or "").strip() or why
        summary["top"] = top_entry

    # 3. Verdict.
    all_blocks_ok = verified_blocks == len(included)
    if scope == "block":
        verdict = "success" if all_blocks_ok else "failed"
        reason = "" if all_blocks_ok else "the block failed server re-verification"
    else:
        if all_blocks_ok and top_ok:
            verdict, reason = "success", ""
        elif verified_blocks >= 1:
            verdict = "partial"
            failed_mods = [
                e.get("block") for e in summary.get("blocks") or []
                if e.get("status") != "verified"
            ]
            bits = []
            if failed_mods:
                bits.append("blocks failed server re-verification: " + ", ".join(failed_mods))
            if not top_ok:
                bits.append(f"top {top_module_name(phy_type)} failed server re-verification")
            reason = "; ".join(bits) + " - verified blocks are still filed"
        else:
            verdict, reason = "failed", "no block passed server re-verification"

    # Persist the server-evaluated summary next to the agent's claim.
    (run_dir / "rtl_summary.server.json").write_text(json.dumps(summary, indent=2))
    return verdict, reason, summary


# ---------------------------------------------------------------------------
# Library filing (rtl / rtl_tb / rtl_log views)


def _file_rtl_cell(
    library: str, cell: str, run_dir: Path, files: dict[str, str],
    run_id: str, spec_ctx: dict[str, Any],
) -> dict[str, Any]:
    """Copy one module's rtl/tb/log views into libraries/<library>/<cell>/,
    merging surviving earlier views in provenance (a cell can hold an rnm
    model AND rtl)."""
    lib_dir = find_library_dir(library) or (libraries_root() / library)
    cell_dir = lib_dir / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for fname in files.values():
        src = run_dir / fname
        if fname and src.is_file():
            shutil.copy2(src, cell_dir / fname)
            copied.append(fname)
    all_views = list(copied)
    prov_path = cell_dir / "provenance.json"
    prov: dict[str, Any] = {}
    if prov_path.exists():
        try:
            prov = json.loads(prov_path.read_text())
        except (json.JSONDecodeError, OSError):
            prov = {}
    for v in prov.get("views") or []:
        if v not in all_views and (cell_dir / v).is_file():
            all_views.append(v)
    prov.update({
        "run_id": run_id,
        "deliverable": "rtl",
        "topology": spec_ctx.get("topology"),
        "label": spec_ctx.get("label"),
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "views": all_views,
    })
    prov_path.write_text(json.dumps(prov, indent=2))
    return {"library": library, "cell": cell, "views": copied, "path": str(cell_dir)}


def file_rtl_run(
    scope: str,
    phy_type: str,
    included: list[dict[str, Any]],
    summary: dict[str, Any],
    library: str,
    cell_override: str | None,
    run_dir: Path,
    run_id: str,
) -> list[dict[str, Any]]:
    """scope=block: files into cell <block> (or the cell override).
    scope=controller: top files into <phy>_controller_top; each VERIFIED
    block additionally filed into its own <block> cell. Failed blocks are
    not filed (their files remain in the run dir for inspection)."""
    filed: list[dict[str, Any]] = []
    entries = {e.get("block"): e for e in summary.get("blocks") or []}
    for b in included:
        mod = module_name(b["id"])
        entry = entries.get(mod) or {}
        if entry.get("status") != "verified":
            continue
        cell = (cell_override if scope == "block" and cell_override else mod)
        filed.append(_file_rtl_cell(
            library, cell, run_dir,
            {"rtl": f"{mod}.v", "rtl_tb": f"{mod}_tb.sv", "rtl_log": f"{mod}_sim.log"},
            run_id, {"topology": b.get("topology"), "label": b.get("label")},
        ))
    if scope == "controller":
        top_entry = summary.get("top") or {}
        if top_entry.get("status") == "verified":
            top = top_module_name(phy_type)
            cell = cell_override or top
            filed.append(_file_rtl_cell(
                library, cell, run_dir,
                {"rtl": f"{top}.v", "rtl_tb": f"{top}_tb.sv", "rtl_log": f"{top}_sim.log"},
                run_id, {"topology": "controller_top", "label": f"{phy_type} controller top"},
            ))
    return filed


# ---------------------------------------------------------------------------
# The whole run


@dataclass
class RtlResult:
    status: str  # "success" | "partial" | "failed"
    reason: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    cost_usd: float | None = None
    duration_ms: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    library_filed: list[dict[str, Any]] = field(default_factory=list)
    library_error: str | None = None


def execute_rtl_run(
    spec: dict[str, Any], run_dir: Path, cancel_event: threading.Event | None = None
) -> RtlResult:
    """Validate was already done at request time; this runs the agent,
    server-enforces the honesty check, and files verified modules. Never
    raises for ordinary failures - the run record carries the reason."""
    scope = spec["scope"]
    phy_type = spec["phy_type"]
    architecture = spec["digital_architecture"]
    iface = normalize_interface(spec["interface"])
    _, if_warnings = validate_interface(iface)
    included, _ = validate_rtl_request(
        scope, phy_type, architecture, spec.get("block_id"),
        spec.get("block_fields"), iface, spec.get("library"), spec.get("cell"),
    )
    status = get_answer_status(phy_type)
    no_spec = status.get("answer") == "no_spec" and not spec.get("spec_doc_ids")
    resolved_docs = resolve_docs_for_generation(phy_type, spec.get("spec_doc_ids") or [])

    prompt = build_rtl_prompt(
        scope, phy_type, architecture, included, spec.get("block_fields"),
        iface, if_warnings, resolved_docs, no_spec,
    )
    (run_dir / "prompt.txt").write_text(prompt)

    started = time.time()
    agent = run_rtl_coder(prompt, run_dir, rtl_timeout_for(scope), cancel_event)
    duration_ms = int((time.time() - started) * 1000)

    if not agent["ok"]:
        return RtlResult(
            status="failed", reason=agent["reason"], raw_text=agent.get("raw_text", ""),
            cost_usd=agent.get("cost_usd"), duration_ms=duration_ms,
            summary={"scope": scope, "phy_type": phy_type},
        )

    verdict, reason, summary = evaluate_rtl_summary(run_dir, scope, phy_type, included)
    summary["scope"] = scope
    summary["phy_type"] = phy_type

    library_filed: list[dict[str, Any]] = []
    library_error = None
    if verdict in ("success", "partial"):
        try:
            library_filed = file_rtl_run(
                scope, phy_type, included, summary, spec["library"],
                spec.get("cell"), run_dir, run_dir.name,
            )
        except (ValueError, OSError) as exc:
            library_error = f"could not file result into library '{spec['library']}': {exc}"
    summary["filed_cells"] = [f"{f['library']}/{f['cell']}" for f in library_filed]

    artifacts = {"rtl_summary": "rtl_summary.json"}
    return RtlResult(
        status=verdict, reason=reason, summary=summary,
        raw_text=agent.get("raw_text", ""), cost_usd=agent.get("cost_usd"),
        duration_ms=duration_ms, artifacts=artifacts,
        library_filed=library_filed, library_error=library_error,
    )


def get_answer_status(phy_type: str) -> dict[str, Any]:
    from spec_docs import get_status  # local import to keep patching simple

    return get_status(phy_type)
