"""
Small local API for the current-mirror spec-entry tool.

Endpoints:
  POST /api/runs          -> create+kick off a run, returns {run_id}
  GET  /api/runs          -> list runs (most recent first)
  GET  /api/runs/{id}     -> run detail (spec, status, summary, log excerpt)
  GET  /api/runs/{id}/file/{name} -> serve a raw artifact (png/spice/log/json)

Runs execute in a background thread (one `claude` subprocess per run) so the
HTTP request returns immediately; the frontend polls GET /api/runs/{id}.

Each run gets its own fresh directory under <working_dir>/runs/<run_id>/
(working_dir is a user setting - see backend/settings.py) so concurrent
or repeated runs never clobber each other or the user's existing
~/sky130-projects/current_mirror/ project (which this code never touches).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

import arch_chat as arch_chat_mod
import fw_chat as fw_chat_mod
import fw_generate as fw_generate_mod
import if_chat as if_chat_mod
import interfaces as interfaces_mod
import rtl_generate as rtl_generate_mod
import spec_docs as spec_docs_mod
from architectures import list_architectures as list_saved_architectures
from architectures import save_architecture
from circuit_builder import LIGHT_DELIVERABLES, run_circuit_builder, timeout_for
from digital_blocks import DIGITAL_BLOCK_GROUPS, DIGITAL_BLOCKS_BY_PHY
from firmware_blocks import (
    FIRMWARE_BLOCK_GROUPS,
    FIRMWARE_BLOCKS_BY_PHY,
    FIRMWARE_MODELS_NOTE,
)
from circuit_researcher import run_circuit_researcher
from libraries import (
    create_library,
    file_generated_symbols,
    file_run_into_library,
    list_libraries,
    validate_library_name,
    validate_stitch_cell_map,
)
from settings import (
    all_libraries_roots,
    all_research_roots,
    all_runs_roots,
    arch_chats_root,
    fw_chats_root,
    if_chats_root,
    research_root,
    runs_root,
    set_working_dir,
    settings_payload,
)
from models_contract import (
    MODEL_APPLICABILITY,
    normalize_arch_blocks,
    toolchain_status,
    validate_arch_model_request,
    validate_models_request,
)
from schematics import (
    DisplayUnavailable,
    LaunchFailed,
    display_status,
    launch_for_resolved,
    resolve_schematic,
    validate_topology_name,
)
from topologies import (
    ALL_TOPOLOGY_VALUES,
    CUSTOM_TOPOLOGY_VALUE,
    TOPOLOGY_GROUPS,
    compute_soft_warnings,
    validate_extra_fields,
)

# sky130's 1.8V-family devices (nfet/pfet_01v8), which every prompt/help text
# in this tool assumes, are usable up to ~1.98 V (1.8 V nominal +10%). Above
# that the 5V family (nfet_g5v0d10v5/pfet_g5v0d10v5) is required - which this
# tool's prompts don't set up - so a higher vdd_v is a hard spec error, not
# something to silently forward. Below ~1.2 V the high pfet_01v8 |Vt|
# (~0.6-0.9 V) makes most topologies headroom-starved; that's a soft warning
# (mirrored in the form), routed into the prompt by circuit_builder.py.
VDD_MAX_1V8_FAMILY = 1.98

# sky130 custom-layout minimum gate WIDTH is 0.42 um (0.36 um only inside
# standard cells, 0.15 um only inside SRAM macros). Only gate LENGTH may go
# down to 0.15 um. The models happily simulate narrower devices, so nothing
# downstream catches a violation - enforce it here.
SKY130_MIN_W_UM = 0.42
SKY130_MIN_L_UM = 0.15


def _validate_common_spec(spec) -> None:
    """Checks shared by RunSpec and ResearchSpec: topology exists, custom name
    present when needed, extra_fields keys/bounds valid (see
    topologies.validate_extra_fields), vdd within the 1.8V device family, and
    cross-field sanity (output swing cannot exceed the supply)."""
    if spec.topology not in ALL_TOPOLOGY_VALUES:
        raise ValueError(
            f"unknown topology '{spec.topology}' - must be one of {sorted(ALL_TOPOLOGY_VALUES)}"
        )
    if spec.topology == CUSTOM_TOPOLOGY_VALUE and not (spec.custom_topology or "").strip():
        raise ValueError("custom_topology is required when topology == 'custom'")
    validate_extra_fields(spec.topology, spec.extra_fields)
    if spec.vdd_v > VDD_MAX_1V8_FAMILY:
        raise ValueError(
            f"vdd_v = {spec.vdd_v} V exceeds the sky130 1.8V device family's usable range "
            f"(~1.2-{VDD_MAX_1V8_FAMILY} V for nfet/pfet_01v8). Supplies above that require the "
            "5V family (nfet_g5v0d10v5/pfet_g5v0d10v5), which this tool's flow does not set up - "
            "lower the supply, or state the 5V-device requirement explicitly in notes with a "
            "vdd_v within range and treat this as a custom flow."
        )
    swing = (spec.extra_fields or {}).get("output_swing_vpp")
    if swing is not None and swing != "":
        try:
            swing_v = float(swing)
        except (TypeError, ValueError):
            swing_v = None
        if swing_v is not None and swing_v > spec.vdd_v:
            raise ValueError(
                f"extra_fields.output_swing_vpp = {swing} Vpp exceeds the supply vdd_v = "
                f"{spec.vdd_v} V - a single-ended output physically cannot swing beyond the rails "
                "(a differential swing up to 2x VDD should be stated as per-side swing plus a "
                "'differential' note in notes)"
            )

def _active_soft_warnings(spec) -> list[str]:
    """All soft (non-blocking) feasibility warnings active for this spec at
    submit time: the schema-driven per-field warn_above/warn_below thresholds
    plus the cross-field checks the form also shows (low-VDD headroom,
    inconsistent serializer/deserializer clock triple). Persisted into the
    run/research record at creation so a result viewed later still carries
    its "this spec was aggressive for sky130" context - and so API users,
    who never see the form's live warnings, get the same flags."""
    warnings = compute_soft_warnings(spec.topology, spec.extra_fields)
    if spec.vdd_v < 1.2:
        warnings.append(
            f"vdd_v = {spec.vdd_v} V is a low supply for sky130: pfet_01v8 |Vt| is "
            "~0.6-0.9 V, so expect lvt devices and reduced stacking headroom."
        )
    ef = spec.extra_fields or {}
    try:
        rate = float(ef.get("serial_data_rate_gbps") or 0)
        width = float(ef.get("parallel_width_bits") or 0)
        clk = float(ef.get("parallel_clock_freq_mhz") or 0)
    except (TypeError, ValueError):
        rate = width = clk = 0
    if rate and width and clk:
        derived = rate * 1000 / width
        if abs(clk - derived) / derived > 0.01:
            warnings.append(
                f"Inconsistent serializer clocking: {rate} Gb/s over {width:g} bits gives a "
                f"{derived:.2f} MHz word clock, not {clk:g} MHz (parallel clock = serial rate / width)."
            )
    return warnings


# Fallback X11 display for launching the xschem GUI (not the headless
# render path, which doesn't need this) - this machine's interactive
# desktop session runs on :2. If the backend's own environment already has
# DISPLAY set, that takes precedence.
FALLBACK_DISPLAY = ":2"

# Where runs/research/libraries live: derived from the user-configurable
# working-dir setting (backend/settings.py), resolved at REQUEST time - not
# module-level constants - so a settings change applies without a restart.
# Existing data at previously-configured working dirs stays readable via the
# all_*_roots() search helpers (current root first, then previous ones).

app = FastAPI(title="SPB-Saraswat PHY Builder")


@app.on_event("startup")
def _on_startup() -> None:
    _load_runs_from_disk()
    _load_research_runs_from_disk()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# run_id -> in-memory state (mirrored to status.json on disk for durability)
_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()

# run_id -> cancellation signal for a currently-running Circuit_Builder
# subprocess. Not persisted (only meaningful while the process is alive);
# entries are removed once the run finishes.
_cancel_events: dict[str, threading.Event] = {}

# Same pair of registries, for Circuit_Researcher runs. Kept entirely
# separate from _runs/_cancel_events (different id space, different
# directory tree, different result shape) rather than shoehorned into the
# same dict, since a research run and a build run are different things with
# different lifecycles (research has no schematic/netlist artifacts).
_research_runs: dict[str, dict] = {}
_research_lock = threading.Lock()
_research_cancel_events: dict[str, threading.Event] = {}


def _load_runs_from_disk() -> None:
    """Repopulate the in-memory run registry from status.json files across
    ALL runs roots (current working dir first, then previous ones). Called on
    startup AND after a working-dir change, so runs already present at a
    newly-pointed location show up without a restart. Ids already tracked in
    memory are left alone - a run that's genuinely running right now is in
    memory, so only untracked on-disk 'running' states are orphans."""
    with _runs_lock:
        for root in all_runs_roots():
            for status_path in sorted(root.glob("*/status.json")):
                run_id_hint = status_path.parent.name
                try:
                    state = json.loads(status_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                run_id = state.get("run_id") or run_id_hint
                if run_id in _runs:
                    continue  # current root wins over previous roots; live runs untouched
                # An untracked run recorded as "running" is orphaned - its
                # subprocess died with a previous backend process - so mark it
                # failed rather than showing it as in-progress forever.
                if state.get("state") == "running":
                    state["state"] = "done"
                    state["status"] = "failed"
                    state["reason"] = "Backend restarted while this run was in progress"
                _runs[run_id] = state


def _load_research_runs_from_disk() -> None:
    with _research_lock:
        for root in all_research_roots():
            for status_path in sorted(root.glob("*/status.json")):
                try:
                    state = json.loads(status_path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                research_id = state.get("research_id") or status_path.parent.name
                if research_id in _research_runs:
                    continue
                if state.get("state") == "running":
                    state["state"] = "done"
                    state["status"] = "failed"
                    state["reason"] = "Backend restarted while this research run was in progress"
                _research_runs[research_id] = state


class ResearchSpec(BaseModel):
    """Subset of RunSpec used to kick off a Circuit_Researcher comparison for
    a topology *category* (the dropdown selection) before a specific
    architecture is chosen and handed to Circuit_Builder. Used for every
    topology, including "nmos_current_mirror" - a plain two-transistor
    mirror is itself just one candidate architecture among cascode/Wilson/
    regulated-cascode/etc. for that category, so it goes through the same
    research step as every other block. Deliberately excludes the
    nmos_current_mirror-only sizing fields (iref_ua, ratio_*, w_ref_um,
    l_ref_um) - those are build-time sizing inputs, not something
    Circuit_Researcher's topology-level comparison needs (same as every
    other topology, whose build-specific fields also aren't sent here)."""

    label: Optional[str] = Field(default=None, description="Optional name for this research request")
    topology: str = Field(description="Topology category to research, from GET /api/topologies")
    custom_topology: Optional[str] = Field(
        default=None, description="Free-text topology name; required and used only when topology == 'custom'"
    )
    # Topology-specific spec fields, keyed by field name from
    # GET /api/topologies's per-option "fields" list (e.g. for "pll":
    # {"ref_freq_mhz": 100, "output_freq_ghz": 2.5, "loop_bandwidth_mhz": 5,
    # "phase_margin_deg": 60}). Replaces the old fixed gain_db/bandwidth_ghz
    # pair, which was wrong-unit or meaningless for most topologies (see
    # ~/.claude/agent-knowledge/circuit-topologies/serdes-phy-spec-fields.md).
    # Empty/omitted for "nmos_current_mirror" (has its own field set above)
    # and "custom" (no fixed field set makes sense there).
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Topology-specific spec fields, keyed by name from GET /api/topologies"
    )
    power_mw: Optional[float] = Field(default=None, gt=0, description="Power budget, mW")
    area_um2: Optional[float] = Field(default=None, gt=0, description="Area budget, um^2")
    notes: Optional[str] = Field(default=None, description="Any other spec detail in free text")
    vdd_v: float = Field(default=1.8, gt=0, description="Supply voltage")
    temp_c: float = Field(default=27.0, ge=-40, le=150, description="Simulation temperature, deg C (ngspice .temp)")
    phy_type: Optional[str] = Field(default="ser-des", description="PHY standard type: ser-des, ddr, lpddr, hbm, optical, die-to-die")
    selected_block: Optional[str] = Field(default=None, description="PHY architecture-diagram block id this spec is for, if any")
    corner: Literal["tt", "ff", "ss", "sf", "fs"] = Field(default="tt", description="sky130 process corner")

    @model_validator(mode="after")
    def _validate_topology(self) -> "ResearchSpec":
        _validate_common_spec(self)
        return self


class RunSpec(BaseModel):
    label: Optional[str] = Field(default=None, description="Optional name for this run")

    # Which circuit block to build. "nmos_current_mirror" is the only
    # topology with a fully fleshed-out, hand-validated prompt/sizing flow
    # (see circuit_builder.build_prompt) - it stays the default so existing
    # behavior for anyone/anything already targeting this API is unchanged.
    # Every other value is a PHY building block from the topology dropdown;
    # see backend/topologies.py for the canonical list served at
    # GET /api/topologies. "custom" means the user typed a free-text
    # topology name themselves (see custom_topology below).
    topology: str = Field(default="nmos_current_mirror", description="Circuit topology/building block to build")
    custom_topology: Optional[str] = Field(
        default=None, description="Free-text topology name; required and used only when topology == 'custom'"
    )

    # Fields specific to the detailed NMOS current-mirror flow. Optional at
    # the model level (Pydantic can't conditionally-require based on a
    # sibling field's value without a validator) but actually required
    # below, via a validator, when topology == 'nmos_current_mirror'.
    iref_ua: Optional[float] = Field(default=10.0, gt=0, description="Reference current in microamps")
    ratio_out: Optional[float] = Field(default=1.0, gt=0, description="Mirror ratio numerator (output side)")
    ratio_ref: Optional[float] = Field(default=1.0, gt=0, description="Mirror ratio denominator (reference side)")
    w_ref_um: Optional[float] = Field(default=2.0, gt=0, description="Reference device width, um")
    l_ref_um: Optional[float] = Field(default=0.5, gt=0, description="Reference device length, um")

    # Topology-specific spec fields, used for every topology other than the
    # current mirror - same key set/meaning as ResearchSpec.extra_fields
    # above (see that field's comment). Circuit_Builder is told to use
    # sensible judgment for anything left blank, same as it already does for
    # sizing details the current-mirror prompt doesn't pin down.
    extra_fields: Optional[dict[str, Any]] = Field(
        default=None, description="Topology-specific spec fields, keyed by name from GET /api/topologies"
    )
    power_mw: Optional[float] = Field(default=None, gt=0, description="Power budget, mW")
    area_um2: Optional[float] = Field(default=None, gt=0, description="Area budget, um^2")
    notes: Optional[str] = Field(default=None, description="Any other spec detail in free text")

    # Populated when this build follows a Circuit_Researcher comparison step
    # (see ResearchSpec / POST /api/research) and the user picked a specific
    # architecture from its candidates - e.g. topology="ctle" (the category)
    # but chosen_architecture="passive single-zero RC CTLE". When set,
    # build_prompt tells Circuit_Builder to build that specific architecture
    # instead of picking its own. Optional/unused for nmos_current_mirror and
    # for anyone who skips the research step.
    chosen_architecture: Optional[str] = Field(
        default=None, description="Specific architecture chosen from Circuit_Researcher's candidates, if any"
    )
    chosen_architecture_rationale: Optional[str] = Field(
        default=None, description="Circuit_Researcher's rationale for chosen_architecture, for context"
    )

    # Common to every topology.
    vdd_v: float = Field(default=1.8, gt=0, description="Supply voltage")
    temp_c: float = Field(default=27.0, ge=-40, le=150, description="Simulation temperature, deg C (ngspice .temp)")
    phy_type: Optional[str] = Field(default="ser-des", description="PHY standard type: ser-des, ddr, lpddr, hbm, optical, die-to-die")
    selected_block: Optional[str] = Field(default=None, description="PHY architecture-diagram block id this spec is for, if any")
    corner: Literal["tt", "ff", "ss", "sf", "fs"] = Field(default="tt", description="sky130 process corner")
    testbench_style: Literal["spice", "xschem"] = Field(
        default="spice",
        description="How Circuit_Builder authors the testbench: a hand-written SPICE file, "
        "or an xschem schematic netlisted headlessly before simulation. Either way it is "
        "named <circuit>_tb.",
    )

    # Behavioral-model generation (cycle 2): which model abstraction levels
    # Circuit_Builder should produce alongside the transistor-level design.
    # Validated against models_contract.MODEL_APPLICABILITY - e.g. asking for
    # digital Verilog of a CTLE (a block with no bits) is a 422, not a
    # silently generated stub. Empty/omitted = no models (the pre-cycle-2
    # behavior, unchanged).
    models: Optional[list[Literal["veriloga", "verilog", "rnm"]]] = Field(
        default=None,
        description="Behavioral model types to generate: any of veriloga, verilog, rnm - "
        "must be applicable to the chosen topology per GET /api/topologies' per-option "
        '"models" matrix',
    )

    # Single-deliverable modes (user request): produce ONLY the chosen
    # deliverable for this block instead of the full design+simulate+models
    # flow. "full" (the default) = current behavior, unchanged.
    #   schematic_only - design + xschem .sch + netlist + rendered PNG; no
    #                    verification sim suite (a quick DC sanity check is
    #                    fine), no behavioral models.
    #   symbol         - just the xschem .sym (pins from the filed library
    #                    schematic when the cell exists, else from the spec).
    #   veriloga/verilog/rnm - just that behavioral model from the spec (no
    #                    transistor-level design), verified with the same
    #                    toolchain contract as full-run models. Subject to
    #                    the same MODEL_APPLICABILITY 422s as the checkboxes.
    deliverable: Literal[
        "full", "schematic_only", "symbol", "veriloga", "verilog", "rnm", "arch_model", "arch_stitch"
    ] = Field(
        default="full",
        description="What this run produces: 'full' (design+simulate+models, the default), "
        "exactly one of schematic_only / symbol / veriloga / verilog / rnm, "
        "'arch_model' (a behavioral model of a whole block diagram / chosen subset - "
        "see architecture/arch_blocks/arch_model_type), or 'arch_stitch' (a top-level "
        "xschem schematic stitching the diagram's blocks together from their filed "
        "library cells - see architecture/arch_blocks/arch_cell_map)",
    )

    # deliverable='arch_model' (user request): behavioral model of the entire
    # displayed block diagram, or of the blocks the user picked from it. The
    # architecture is the same {blocks, edges} shape the arch-chat endpoint
    # takes; blocks with topology=null (pads etc.) are drawn-only and are
    # skipped. RNM is the default/only universally applicable level; 'verilog'
    # is allowed when every included block's topology supports it.
    architecture: Optional[dict[str, Any]] = Field(
        default=None,
        description='For deliverable="arch_model": {"blocks": [{"id","label","topology",...}], '
        '"edges": [{"from","to"}]} - the diagram to model',
    )
    arch_blocks: Optional[list[str]] = Field(
        default=None,
        description='For deliverable="arch_model": block ids to include; omitted/empty = the entire diagram',
    )
    arch_model_type: Literal["rnm", "verilog"] = Field(
        default="rnm",
        description='For deliverable="arch_model": model level for the composite model (rnm is '
        "applicable to every block type; verilog only when all included blocks are clocked/bit-level)",
    )
    # deliverable='arch_stitch': WHERE each individual block is placed from -
    # {block_id: "<library>/<cell>"}. Every included designable block must map
    # to an existing filed cell with a symbol view; the top-level stitch
    # itself goes to `library`/`cell` below (asked of the user in the UI).
    arch_cell_map: Optional[dict[str, str]] = Field(
        default=None,
        description='For deliverable="arch_stitch": {block_id: "<library>/<cell>"} - the filed '
        "cell each block instance is placed from (must exist and have a .sym view)",
    )

    # Target library cell (user request): when the user picked an EXISTING
    # cell of the chosen library to work on, the run's views are filed into
    # that cell (adding/refreshing views) instead of a cell named after the
    # schematic; symbol runs read their pin source from it. None = the run
    # names its own (possibly new) cell, the pre-existing behavior.
    cell: Optional[str] = Field(
        default=None,
        description="Existing library cell to file this run's views into (letters/digits/_/-); "
        "omit to let the run create/name its own cell",
    )

    # Design library (library/cell/view structure) this block is filed into on success (user
    # request): the run's design views (schematic/symbol/netlist/testbench/
    # PNG/behavioral models/summary) are copied to libraries/<library>/<cell>/,
    # which is on every run's XSCHEM_LIBRARY_PATH so filed cells are
    # browsable/instantiable from xschem. Optional at the API level (None =
    # keep the pre-existing behavior of leaving results only in the run dir);
    # the form always asks for one. Created implicitly if it doesn't exist
    # yet ("choose existing or create new" both land here).
    library: Optional[str] = Field(
        default=None,
        description="Design library (libraries/<name>/) to file this block into on success; "
        "letters/digits/_/-, created if missing",
    )

    @model_validator(mode="after")
    def _validate_topology(self) -> "RunSpec":
        if self.cell is not None:
            validate_library_name(self.cell)  # same charset/traversal rules as libraries
        if self.deliverable in ("arch_model", "arch_stitch"):
            # Architecture-wide runs are validated on the diagram they carry,
            # not on the (ignored) single-topology fields.
            if self.models:
                raise ValueError(
                    f"models checkboxes do not apply to an {self.deliverable} run"
                )
            if self.library is not None:
                validate_library_name(self.library)
            if self.deliverable == "arch_model":
                validate_arch_model_request(self.architecture, self.arch_blocks, self.arch_model_type)
            else:
                # Stitch: every included block must be placed from an existing
                # filed cell with a symbol view; the top-level stitch needs a
                # destination library (the "where should it go" answer).
                included = normalize_arch_blocks(self.architecture, self.arch_blocks)
                if not self.library:
                    raise ValueError(
                        "deliverable='arch_stitch' requires a destination library "
                        "(where the top-level stitch schematic is filed)"
                    )
                validate_stitch_cell_map(included, self.arch_cell_map)
            return self
        if self.architecture is not None or self.arch_blocks or self.arch_cell_map:
            raise ValueError(
                "architecture/arch_blocks/arch_cell_map are only meaningful with "
                "deliverable='arch_model' or 'arch_stitch'"
            )
        _validate_common_spec(self)
        validate_models_request(self.topology, self.models)
        # Model-only deliverables go through the SAME applicability matrix as
        # the checkboxes (e.g. a digital-Verilog-only run for a CTLE is a
        # 422 with the same reason string, not a silently generated stub).
        if self.deliverable in ("veriloga", "verilog", "rnm"):
            validate_models_request(self.topology, [self.deliverable])
        # The models checkboxes only make sense on a full run - any other
        # mode produces exactly one deliverable, so a spec carrying both is
        # contradictory and rejected rather than partially honored.
        if self.deliverable != "full" and self.models:
            raise ValueError(
                f"models {list(self.models)} can only be requested on a deliverable='full' run - "
                f"deliverable='{self.deliverable}' produces exactly one output "
                "(use deliverable='veriloga'/'verilog'/'rnm' for a single model-only run)"
            )
        if self.library is not None:
            validate_library_name(self.library)
        if self.topology == "nmos_current_mirror":
            missing = [
                name
                for name in ("iref_ua", "ratio_out", "ratio_ref", "w_ref_um", "l_ref_um")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"topology 'nmos_current_mirror' requires: {', '.join(missing)}")
            # sky130 DRC width/length floor - the models simulate narrower
            # devices without complaint, so a "successful" run would hide a
            # DRC-illegal design if we let these through.
            if self.w_ref_um < SKY130_MIN_W_UM:
                raise ValueError(
                    f"w_ref_um = {self.w_ref_um} um is below sky130's custom-layout minimum gate "
                    f"width of {SKY130_MIN_W_UM} um (0.15 um applies to gate LENGTH only)"
                )
            if self.l_ref_um < SKY130_MIN_L_UM:
                raise ValueError(
                    f"l_ref_um = {self.l_ref_um} um is below sky130's minimum gate length of "
                    f"{SKY130_MIN_L_UM} um"
                )
            w_mirror_um = self.w_ref_um * self.ratio_out / self.ratio_ref
            if w_mirror_um < SKY130_MIN_W_UM:
                raise ValueError(
                    f"derived mirror device width w_ref_um * ratio_out / ratio_ref = "
                    f"{w_mirror_um:.4g} um is below sky130's minimum gate width of "
                    f"{SKY130_MIN_W_UM} um - widen the reference device or change the ratio"
                )
        return self


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_run_dir(run_id: str) -> Optional[Path]:
    """Locate an existing run's directory: current runs root first, then
    previous working dirs' roots. Runs are never moved when the working dir
    changes, so a run started before the change keeps living (and writing its
    status) at its original location."""
    for root in all_runs_roots():
        candidate = root / run_id
        if candidate.is_dir():
            return candidate
    return None


def _write_status(run_id: str, run_dir: Path) -> None:
    state = _runs[run_id]
    (run_dir / "status.json").write_text(json.dumps(state, indent=2, default=str))


def _execute(run_id: str, run_dir: Path, spec: dict, cancel_event: threading.Event) -> None:
    try:
        # Timeout scales with the deliverable: symbol/model-only runs are far
        # cheaper than a full design+simulate flow and should fail fast
        # rather than hang for the full-run 40 minutes.
        result = run_circuit_builder(
            spec, run_dir, timeout_s=timeout_for(spec.get("deliverable")), cancel_event=cancel_event
        )
        # File a successful run into its chosen design library (hierarchical
        # lib/cell/view directory tree browsable from xschem). Failures here
        # (disk trouble, bad name that slipped past validation) are recorded
        # on the run instead of being raised - the build itself succeeded and
        # its run dir is intact, so don't report the whole run as failed.
        library_filed = None
        library_error = None
        symbols_filed = None
        if result.status == "success" and spec.get("library"):
            try:
                library_filed = file_run_into_library(
                    spec["library"], run_id, run_dir, spec, result.summary
                )
            except (ValueError, OSError) as exc:
                library_error = f"could not file result into library '{spec['library']}': {exc}"
        # arch_stitch runs may have generated missing block symbols (user
        # request): file each back into its block's OWN library cell so the
        # next stitch/xschem session finds it as <lib>/<cell>/<cell>.sym.
        if result.status == "success" and spec.get("deliverable") == "arch_stitch":
            try:
                symbols_filed = file_generated_symbols(
                    spec.get("arch_cell_map"),
                    (result.summary or {}).get("symbols_generated"),
                    run_dir,
                )
            except OSError as exc:
                library_error = (
                    f"{library_error + '; ' if library_error else ''}"
                    f"could not back-file generated symbols: {exc}"
                )
        with _runs_lock:
            _runs[run_id].update(
                state="done",
                status=result.status,
                reason=result.reason,
                summary=result.summary,
                raw_text=result.raw_text,
                session_id=result.session_id,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                artifacts=result.artifacts,
                library_filed=library_filed,
                library_error=library_error,
                symbols_filed=symbols_filed,
                finished_at=_now(),
            )
    except Exception as exc:  # noqa: BLE001 - a real failure mode we must surface, not hide
        with _runs_lock:
            _runs[run_id].update(
                state="done",
                status="failed",
                reason=f"Unexpected backend error: {exc}",
                finished_at=_now(),
            )
    finally:
        _cancel_events.pop(run_id, None)
    _write_status(run_id, run_dir)


def _find_research_dir(research_id: str) -> Optional[Path]:
    for root in all_research_roots():
        candidate = root / research_id
        if candidate.is_dir():
            return candidate
    return None


def _write_research_status(research_id: str, run_dir: Path) -> None:
    state = _research_runs[research_id]
    (run_dir / "status.json").write_text(json.dumps(state, indent=2, default=str))


def _execute_research(research_id: str, run_dir: Path, spec: dict, cancel_event: threading.Event) -> None:
    try:
        result = run_circuit_researcher(spec, run_dir, cancel_event=cancel_event)
        with _research_lock:
            _research_runs[research_id].update(
                state="done",
                status=result.status,
                reason=result.reason,
                summary=result.summary,
                raw_text=result.raw_text,
                session_id=result.session_id,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                finished_at=_now(),
            )
    except Exception as exc:  # noqa: BLE001 - a real failure mode we must surface, not hide
        with _research_lock:
            _research_runs[research_id].update(
                state="done",
                status="failed",
                reason=f"Unexpected backend error: {exc}",
                finished_at=_now(),
            )
    finally:
        _research_cancel_events.pop(research_id, None)
    _write_research_status(research_id, run_dir)


@app.post("/api/research")
def create_research(spec: ResearchSpec):
    """Kick off a Circuit_Researcher comparison of architectures within the
    chosen topology *category* (e.g. "ctle"). Returns immediately with a
    {research_id} the frontend polls via GET /api/research/{id}; once done,
    the user picks one of the returned candidates and that choice is passed
    as chosen_architecture on the subsequent POST /api/runs."""
    research_id = uuid.uuid4().hex[:12]
    # New research runs always land under the CURRENT working dir's
    # research_runs/ root, resolved from settings at request time.
    run_dir = research_root() / research_id
    run_dir.mkdir(parents=True, exist_ok=False)
    spec_dict = spec.model_dump()
    (run_dir / "spec.json").write_text(json.dumps(spec_dict, indent=2))

    with _research_lock:
        _research_runs[research_id] = {
            "research_id": research_id,
            "label": spec.label,
            "spec": spec_dict,
            # Soft sky130-feasibility warnings active at submit time (see
            # _active_soft_warnings) - persisted so the result keeps its
            # "this spec was aggressive" context when viewed later.
            "warnings": _active_soft_warnings(spec),
            "state": "running",
            "status": None,
            "created_at": _now(),
            "finished_at": None,
        }
    _write_research_status(research_id, run_dir)

    cancel_event = threading.Event()
    _research_cancel_events[research_id] = cancel_event
    thread = threading.Thread(
        target=_execute_research, args=(research_id, run_dir, spec_dict, cancel_event), daemon=True
    )
    thread.start()

    return {"research_id": research_id}


@app.post("/api/research/{research_id}/cancel")
def cancel_research(research_id: str):
    with _research_lock:
        research = _research_runs.get(research_id)
        if research is None:
            raise HTTPException(status_code=404, detail="research run not found")
        if research["state"] != "running":
            raise HTTPException(status_code=409, detail="research run is not currently running")
    event = _research_cancel_events.get(research_id)
    if event is None:
        raise HTTPException(status_code=409, detail="research run cannot be cancelled (no active process)")
    event.set()
    return {"status": "cancelling"}


@app.get("/api/research/{research_id}")
def get_research(research_id: str):
    with _research_lock:
        research = _research_runs.get(research_id)
        if research is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return dict(research)


@app.get("/api/research/{research_id}/file/{name}")
def get_research_file(research_id: str, name: str):
    run_dir = _find_research_dir(research_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="research run not found")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = (run_dir / name).resolve()
    if run_dir.resolve() not in path.parents and path != run_dir.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Architecture-discussion chat (drawing tab). Contract for the sidebar UI:
# audits/2026-08-21-arch-chat-feature.md. The consultant invocation itself
# lives in backend/arch_chat.py (isolated, monkeypatchable in tests).


class ArchChatRequest(BaseModel):
    session_id: str = Field(
        description="Client-generated chat session id (1-64 chars of [A-Za-z0-9_-]); "
        "the transcript persists server-side under arch_chats/<session_id>.json"
    )
    message: str = Field(min_length=1, description="The user's chat message")
    architecture: dict[str, Any] = Field(
        description="The architecture as the frontend currently holds it: "
        '{"blocks": [{"id", "label", "topology", ...}], "edges": [{"from", "to"}]} '
        '("connections" accepted as an alias for "edges")'
    )
    phy_type: Optional[str] = Field(
        default=None, description="PHY type of the displayed architecture, for prompt context"
    )
    # Which workspace's diagram this chat is about (2026-08-23 digital-arch
    # spec): "afe" (default - the pre-existing behavior, analog topology
    # catalog) or "digital" (digital block catalog, kind "iface" anchors,
    # digital-microarchitecture prompt framing).
    view: Literal["afe", "digital"] = Field(
        default="afe",
        description='Diagram the chat is about: "afe" (analog catalog, default) or "digital" '
        "(digital block catalog; patch topologies validate against GET /api/digital_blocks)",
    )
    afe_architecture: Optional[dict[str, Any]] = Field(
        default=None,
        description='For view="digital": the AFE architecture as READ-ONLY prompt context, so '
        "the consultant keeps the two sides consistent (ignored for the afe view)",
    )
    # Spec-document intake (2026-08-23 rtl-gen spec, section a): metadata
    # objects of the attached docs whose applies_to_workspaces includes this
    # workspace. Rendered into a compact block (title/family/version/notes/
    # section locators, 2000-char cap) - document CONTENTS are never inlined.
    spec_docs: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description='For view="digital": spec-doc metadata objects for prompt context '
        "(metadata + section locators only, never document contents)",
    )


@app.post("/api/arch_chat")
def post_arch_chat(body: ArchChatRequest):
    """One architecture-chat turn: sends the current architecture + the last
    few transcript turns + the user's message to a headless
    Circuit_Researcher consultant session (a paid claude invocation -
    synchronous; the frontend awaits this call, typically 20-90 s).
    Response: {session_id, reply, patch, patch_valid, patch_errors,
    cost_usd, duration_ms}. `patch` is a validated {"ops": [...]} edit
    proposal or null; a malformed patch from the model NEVER fails the chat
    (patch is null and the reply text still comes back)."""
    try:
        arch_chat_mod.validate_session_id(body.session_id)
        architecture = arch_chat_mod.normalize_architecture(body.architecture)
        afe_context = (
            arch_chat_mod.normalize_architecture(body.afe_architecture)
            if body.view == "digital" and body.afe_architecture is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    chats_root = arch_chats_root()
    transcript = arch_chat_mod.load_transcript(chats_root, body.session_id)

    prompt = arch_chat_mod.build_chat_prompt(
        architecture,
        transcript["turns"],
        body.message,
        phy_type=body.phy_type,
        view=body.view,
        afe_architecture=afe_context,
        spec_docs_context=(
            spec_docs_mod.render_chat_context(body.spec_docs)
            if body.view == "digital"
            else None
        ),
    )
    # Per-session debug artifacts (prompt/raw stream) land in
    # arch_chats/<session_id>_logs/, overwritten each turn.
    result = arch_chat_mod.run_arch_consultant(
        prompt, chats_root / f"{body.session_id}_logs", "last_turn"
    )

    if result.status != "success":
        # The turn failed before producing a reply - surface it plainly and
        # do NOT record a fake exchange in the transcript.
        raise HTTPException(
            status_code=502,
            detail=f"architecture consultant failed: {result.reason}",
        )

    reply, patch = arch_chat_mod.parse_reply_and_patch(result.raw_text)
    patch_valid = True
    patch_errors: list[dict] = []
    patch_warnings: list[dict] = []
    if patch is not None:
        annotated_ops, patch_valid = arch_chat_mod.validate_patch(
            patch, architecture, view=body.view, phy_type=body.phy_type
        )
        patch = {"ops": annotated_ops}
        patch_errors = [
            {"index": i, "error": op["error"]}
            for i, op in enumerate(annotated_ops)
            if op.get("error")
        ]
        patch_warnings = [
            {"index": i, "warning": op["warning"]}
            for i, op in enumerate(annotated_ops)
            if op.get("warning")
        ]

    arch_chat_mod.append_turns(
        chats_root,
        body.session_id,
        {"role": "user", "content": body.message, "ts": _now()},
        {"role": "assistant", "content": reply, "patch": patch, "ts": _now()},
    )

    return {
        "session_id": body.session_id,
        "reply": reply,
        "patch": patch,
        "patch_valid": patch_valid,
        "patch_errors": patch_errors,
        "patch_warnings": patch_warnings,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/arch_chat/{session_id}")
def get_arch_chat(session_id: str):
    """The persisted transcript for one chat session ({session_id,
    created_at, turns: [{role, content, patch?, ts}]}) - lets the sidebar
    restore history after a reload. A never-seen session id returns an
    empty transcript, not a 404 (ids are client-generated)."""
    try:
        arch_chat_mod.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return arch_chat_mod.load_transcript(arch_chats_root(), session_id)


class ArchitectureSave(BaseModel):
    name: str = Field(description="Unique name (1-64 chars of [A-Za-z0-9_-]); saving an existing name overwrites it")
    architecture: dict[str, Any] = Field(description='{"blocks": [...], "edges": [...]}')
    phy_type: Optional[str] = Field(default=None, description="PHY type this architecture derives from, if any")
    label: Optional[str] = Field(default=None, description="Human-readable display label (defaults to name)")


@app.get("/api/architectures")
def get_architectures():
    """User-saved custom architectures (architectures/<name>.json under the
    working dir, previous working dirs still readable), newest first. The
    BUILT-IN architectures are frontend-static (phyArchitectures.js); the
    frontend merges this list alongside them."""
    return {"architectures": list_saved_architectures()}


@app.post("/api/architectures")
def post_architecture(body: ArchitectureSave):
    """Save (or overwrite) a named custom architecture. Returns the stored
    entry: {name, label, phy_type, created_at, updated_at?, architecture}."""
    try:
        return save_architecture(body.name, body.architecture, body.phy_type, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ---------------------------------------------------------------------------
# Digital block catalog + AFE<->digital interface workspace (2026-08-23
# digital-arch spec). Data model/validation/patch ops: backend/interfaces.py;
# consultant prompt/invocation: backend/if_chat.py.


@app.get("/api/digital_blocks")
def get_digital_blocks():
    """Digital block catalog for the digital-architecture view - exact same
    shape as GET /api/topologies (groups -> options with "fields" and
    "models"), plus the per-PHY applicability map. A SEPARATE namespace from
    the analog topologies: these values validate digital diagrams
    (arch_chat view="digital") and are never handed to Circuit_Builder's
    analog flow."""
    return {"groups": DIGITAL_BLOCK_GROUPS, "by_phy": DIGITAL_BLOCKS_BY_PHY}


class InterfaceSave(BaseModel):
    name: str = Field(description="Unique name (1-64 chars of [A-Za-z0-9_-]); saving an existing name overwrites it")
    interface: dict[str, Any] = Field(
        description="The interface definition: {name, phy_type, clock_domains, signals, "
        "cdc_points, reset_sequence, power_sequence}"
    )
    phy_type: Optional[str] = Field(default=None, description="PHY type this interface belongs to")
    label: Optional[str] = Field(default=None, description="Human-readable display label (defaults to name)")


@app.get("/api/interfaces")
def get_interfaces():
    """User-saved interface definitions (interfaces/<name>.json under the
    working dir, previous working dirs still readable), newest first. The
    BUILT-IN per-PHY defaults are frontend-static (defaultInterfaces.js),
    same split as architectures."""
    return {"interfaces": interfaces_mod.list_interfaces()}


@app.post("/api/interfaces")
def post_interface(body: InterfaceSave):
    """Save (or overwrite) a named interface definition. Hard validation
    errors (see interfaces.validate_interface) are a 422; soft warnings are
    returned in the stored entry's "warnings" list, never blocking."""
    try:
        return interfaces_mod.save_interface(body.name, body.interface, body.phy_type, body.label)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class IfChatRequest(BaseModel):
    session_id: str = Field(
        description="Client-generated chat session id (1-64 chars of [A-Za-z0-9_-]); "
        "the transcript persists server-side under if_chats/<session_id>.json"
    )
    message: str = Field(min_length=1, description="The user's chat message")
    interface: dict[str, Any] = Field(
        description="The full interface definition exactly as the editor holds it"
    )
    afe_architecture: Optional[dict[str, Any]] = Field(
        default=None, description="AFE diagram {blocks, edges} - READ-ONLY prompt context"
    )
    digital_architecture: Optional[dict[str, Any]] = Field(
        default=None, description="Digital diagram {blocks, edges} - READ-ONLY prompt context"
    )
    phy_type: Optional[str] = Field(default=None, description="PHY type, for prompt context")
    spec_docs: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Spec-doc metadata objects for prompt context (metadata + section "
        "locators only, never document contents; 2000-char cap)",
    )


@app.post("/api/if_chat")
def post_if_chat(body: IfChatRequest):
    """One interface-consultant turn - same envelope/session/timeout/error
    semantics as POST /api/arch_chat (synchronous, 20-90 s, 502 on a failed
    invocation, malformed patch never fails the chat). The patch ops are the
    interface set (interfaces.apply_if_patch); both architectures are
    read-only context. Response additionally carries "interface_warnings":
    the soft warnings of the definition AFTER a valid patch would be applied
    (or of the submitted definition when there is no patch)."""
    try:
        if_chat_mod.validate_session_id(body.session_id)
        interface = interfaces_mod.normalize_interface(body.interface)
        afe_arch = (
            arch_chat_mod.normalize_architecture(body.afe_architecture)
            if body.afe_architecture is not None
            else None
        )
        dig_arch = (
            arch_chat_mod.normalize_architecture(body.digital_architecture)
            if body.digital_architecture is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    chats_root = if_chats_root()
    transcript = if_chat_mod.load_transcript(chats_root, body.session_id)

    prompt = if_chat_mod.build_if_chat_prompt(
        interface, afe_arch, dig_arch, transcript["turns"], body.message,
        phy_type=body.phy_type,
        spec_docs_context=spec_docs_mod.render_chat_context(body.spec_docs),
    )
    result = if_chat_mod.run_if_consultant(
        prompt, chats_root / f"{body.session_id}_logs", "last_turn"
    )

    if result.status != "success":
        raise HTTPException(
            status_code=502,
            detail=f"interface consultant failed: {result.reason}",
        )

    reply, patch = if_chat_mod.parse_reply_and_patch(result.raw_text)
    patch_valid = True
    patch_errors: list[dict] = []
    resulting = interface
    if patch is not None:
        annotated_ops, patch_valid, resulting = interfaces_mod.apply_if_patch(patch, interface)
        patch = {"ops": annotated_ops}
        patch_errors = [
            {"index": i, "error": op["error"]}
            for i, op in enumerate(annotated_ops)
            if op.get("error")
        ]
    _, interface_warnings = interfaces_mod.validate_interface(resulting, afe_arch, dig_arch)

    if_chat_mod.append_turns(
        chats_root,
        body.session_id,
        {"role": "user", "content": body.message, "ts": _now()},
        {"role": "assistant", "content": reply, "patch": patch, "ts": _now()},
    )

    return {
        "session_id": body.session_id,
        "reply": reply,
        "patch": patch,
        "patch_valid": patch_valid,
        "patch_errors": patch_errors,
        "interface_warnings": interface_warnings,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/if_chat/{session_id}")
def get_if_chat(session_id: str):
    """Persisted transcript for one interface-chat session; a never-seen id
    returns an empty transcript, not a 404 (ids are client-generated)."""
    try:
        if_chat_mod.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return if_chat_mod.load_transcript(if_chats_root(), session_id)


# ---------------------------------------------------------------------------
# Firmware workspace (2026-08-23 firmware-section spec). Catalog:
# backend/firmware_blocks.py; consultant prompt: backend/fw_chat.py; the
# register-layer codegen + server-enforced honesty check:
# backend/fw_generate.py.


@app.get("/api/firmware_blocks")
def get_firmware_blocks():
    """Firmware block catalog for the firmware workspace - same {groups,
    by_phy} shape as GET /api/digital_blocks, with deliberately NO "models"
    key on any option (see the "note" in the payload): firmware is
    host-compiled C verified by gcc + unit tests, not behavioral-model
    levels. A separate namespace from both the analog topologies and the
    digital blocks."""
    return {
        "groups": FIRMWARE_BLOCK_GROUPS,
        "by_phy": FIRMWARE_BLOCKS_BY_PHY,
        "note": FIRMWARE_MODELS_NOTE,
    }


class FwChatRequest(BaseModel):
    session_id: str = Field(
        description="Client-generated chat session id (1-64 chars of [A-Za-z0-9_-]); "
        "the transcript persists server-side under fw_chats/<session_id>.json"
    )
    message: str = Field(min_length=1, description="The user's chat message")
    firmware_architecture: dict[str, Any] = Field(
        description='The firmware diagram as the frontend holds it: {"blocks": [...], "edges": [...]}'
    )
    interface: Optional[dict[str, Any]] = Field(
        default=None,
        description="The stored interface definition - READ-ONLY prompt context (the source of "
        "truth for what firmware can reach; the patch can only touch the firmware diagram)",
    )
    phy_type: Optional[str] = Field(default=None, description="PHY type, for prompt context")
    spec_docs: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Spec-doc metadata objects for prompt context (metadata + section "
        "locators only, never document contents; 2000-char cap)",
    )


@app.post("/api/fw_chat")
def post_fw_chat(body: FwChatRequest):
    """One firmware-consultant turn (Firmware_Coder persona) - same
    envelope/session/timeout/error semantics as POST /api/arch_chat
    (synchronous, 20-90 s, 502 on a failed invocation, malformed patch never
    fails the chat). Patch ops are the arch_chat diagram set validated
    against the FIRMWARE catalog (kind "iface" allowed, off-per-PHY
    topologies soft-warned via patch_warnings); the interface definition is
    read-only context. The controller architecture is deliberately not part
    of the request (the interface already encodes what firmware can reach)."""
    try:
        fw_chat_mod.validate_session_id(body.session_id)
        architecture = arch_chat_mod.normalize_architecture(body.firmware_architecture)
        iface_ctx = (
            interfaces_mod.normalize_interface(body.interface)
            if body.interface is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    chats_root = fw_chats_root()
    transcript = fw_chat_mod.load_transcript(chats_root, body.session_id)

    prompt = fw_chat_mod.build_fw_chat_prompt(
        architecture, iface_ctx, transcript["turns"], body.message,
        phy_type=body.phy_type,
        spec_docs_context=spec_docs_mod.render_chat_context(body.spec_docs),
    )
    result = fw_chat_mod.run_fw_consultant(
        prompt, chats_root / f"{body.session_id}_logs", "last_turn"
    )

    if result.status != "success":
        raise HTTPException(
            status_code=502,
            detail=f"firmware consultant failed: {result.reason}",
        )

    reply, patch = fw_chat_mod.parse_reply_and_patch(result.raw_text)
    patch_valid = True
    patch_errors: list[dict] = []
    patch_warnings: list[dict] = []
    if patch is not None:
        annotated_ops, patch_valid = arch_chat_mod.validate_patch(
            patch, architecture, view="firmware", phy_type=body.phy_type
        )
        patch = {"ops": annotated_ops}
        patch_errors = [
            {"index": i, "error": op["error"]}
            for i, op in enumerate(annotated_ops)
            if op.get("error")
        ]
        patch_warnings = [
            {"index": i, "warning": op["warning"]}
            for i, op in enumerate(annotated_ops)
            if op.get("warning")
        ]

    fw_chat_mod.append_turns(
        chats_root,
        body.session_id,
        {"role": "user", "content": body.message, "ts": _now()},
        {"role": "assistant", "content": reply, "patch": patch, "ts": _now()},
    )

    return {
        "session_id": body.session_id,
        "reply": reply,
        "patch": patch,
        "patch_valid": patch_valid,
        "patch_errors": patch_errors,
        "patch_warnings": patch_warnings,
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/fw_chat/{session_id}")
def get_fw_chat(session_id: str):
    """Persisted transcript for one firmware-chat session (fw_chats/
    namespace); a never-seen id returns an empty transcript, not a 404."""
    try:
        fw_chat_mod.validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return fw_chat_mod.load_transcript(fw_chats_root(), session_id)


class FwGenerateRequest(BaseModel):
    phy_type: str = Field(description="PHY type the firmware targets (sets firmware/<phy_type>/)")
    interface: dict[str, Any] = Field(
        description="The interface definition exactly as the editor holds it - the source the "
        "register layer is generated from"
    )


@app.post("/api/fw_generate")
def post_fw_generate(body: FwGenerateRequest):
    """The one v1 firmware codegen action: a SYNCHRONOUS headless
    Firmware_Coder run (a paid claude invocation, typically minutes)
    generating regs header + driver + stub register model + host tests +
    Makefile into <working_dir>/firmware/<phy_type>/. The pass/fail in the
    response is SERVER-ENFORCED: the backend itself re-runs
    `gcc -std=c99 -Wall -Werror` and `make test` and reports "pass" only on
    exit 0 - the agent's claim is never trusted. On failure the artifacts are
    kept and the compile/test log tail comes back in test_output."""
    try:
        result = fw_generate_mod.fw_generate(body.phy_type, body.interface)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {
        "status": result.status,
        "run_id": result.run_id,
        "reason": result.reason,
        "files": result.files,
        "missing_files": result.missing_files,
        "compile_ok": result.compile_ok,
        "tests_ok": result.tests_ok,
        "test_output": result.test_output,
        "cost_usd": result.cost_usd,
        "duration_s": result.duration_s,
    }


# ---------------------------------------------------------------------------
# Specification-document intake + RTL generation (2026-08-23 rtl-gen spec).
# Storage/validation/context rendering: backend/spec_docs.py; the rtl
# deliverable (RTL_Coder invocation + server-enforced re-execution honesty
# check): backend/rtl_generate.py.


class SpecDocCreate(BaseModel):
    phy_type: str = Field(description="PHY type the document belongs to")
    metadata: dict[str, Any] = Field(
        description="Doc metadata: {title, standard_family, version, source: "
        '{type: "file"|"url"|"reference", path|url|citation}, user_notes, '
        "relevant_sections: [{label, locator, applies_to}], applies_to_workspaces}. "
        'source.type "file" carries an absolute local path the backend copies into '
        "spec_docs/<phy>/ (server-side path attach - backend and browser share this machine)"
    )


class SpecDocUpdate(BaseModel):
    metadata: dict[str, Any] = Field(
        description="Fields to merge over the stored metadata (relevant_sections is the "
        "common edit); the stored file and the id are untouched"
    )


class SpecDocAnswer(BaseModel):
    answer: Literal["attached", "no_spec"] = Field(
        description='The per-PHY spec-doc answer; "no_spec" suppresses the intake banner'
    )


@app.get("/api/spec_docs")
def get_spec_docs(phy_type: Optional[str] = None):
    """All spec-doc metadata (newest first) + the per-PHY _status answer
    records, across working-dir roots. ?phy_type= narrows to one PHY."""
    if phy_type is not None:
        try:
            spec_docs_mod.validate_phy_type(phy_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    return spec_docs_mod.list_spec_docs(phy_type)


@app.post("/api/spec_docs")
def post_spec_doc(body: SpecDocCreate):
    """Attach one spec doc: file sources are COPIED into spec_docs/<phy>/
    (40 MB cap; pdf/txt/md/html readable by the generation agent, other
    types stored but marked unreadable_by_agent); url/reference sources are
    metadata-only. 422 lists every metadata problem at once. Also records
    the per-PHY answer as "attached" (banner suppression)."""
    try:
        return spec_docs_mod.save_spec_doc(body.phy_type, body.metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.put("/api/spec_docs/{phy_type}/{doc_id}")
def put_spec_doc(phy_type: str, doc_id: str, body: SpecDocUpdate):
    try:
        return spec_docs_mod.update_spec_doc(phy_type, doc_id, body.metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@app.delete("/api/spec_docs/{phy_type}/{doc_id}")
def delete_spec_doc(phy_type: str, doc_id: str):
    try:
        spec_docs_mod.delete_spec_doc(phy_type, doc_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"deleted": f"{phy_type}/{doc_id}"}


@app.post("/api/spec_docs/{phy_type}/answer")
def post_spec_doc_answer(phy_type: str, body: SpecDocAnswer):
    """Set the per-PHY _status answer ("no_spec" or "attached") - an explicit
    choice means the intake banner never auto-asks again for this PHY."""
    try:
        return spec_docs_mod.set_answer(phy_type, body.answer)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


class RtlGenerateRequest(BaseModel):
    scope: Literal["block", "controller"] = Field(
        description="'block' = one designable diagram block; 'controller' = every "
        "designable block plus the stitched <phy>_controller_top"
    )
    phy_type: str = Field(description="PHY type of the controller")
    digital_architecture: dict[str, Any] = Field(
        description='The current effective digital diagram: {"blocks": [...], "edges": [...]}'
    )
    block_id: Optional[str] = Field(
        default=None, description="scope='block' only: the designable diagram block to generate"
    )
    block_fields: Optional[dict[str, Any]] = Field(
        default=None,
        description="Catalog field values per block id ({block_id: {field: value}}) - the "
        "dig-fields-<phy> localStorage record; fields with a catalog min and no value are "
        "required (422)",
    )
    interface: dict[str, Any] = Field(
        description="The full interface definition as stored - the AFE-boundary port "
        "contract (hard validation errors block the run; warnings pass into the prompt)"
    )
    spec_doc_ids: Optional[list[str]] = Field(
        default=None,
        description="Attached spec-doc ids to pass as requirements source (resolved "
        "server-side to metadata + file paths; unknown ids are a 422)",
    )
    library: Optional[str] = Field(
        default=None, description="REQUIRED: filing destination library for verified RTL"
    )
    cell: Optional[str] = Field(
        default=None,
        description="Optional cell override (defaults: scope=block -> the block's module "
        "name; scope=controller top -> <phy>_controller_top)",
    )


def _execute_rtl(run_id: str, run_dir: Path, spec: dict, cancel_event: threading.Event) -> None:
    try:
        result = rtl_generate_mod.execute_rtl_run(spec, run_dir, cancel_event=cancel_event)
        with _runs_lock:
            _runs[run_id].update(
                state="done",
                status=result.status,
                reason=result.reason,
                summary=result.summary,
                raw_text=result.raw_text,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
                artifacts=result.artifacts,
                library_filed=result.library_filed or None,
                library_error=result.library_error,
                finished_at=_now(),
            )
    except Exception as exc:  # noqa: BLE001 - a real failure mode we must surface, not hide
        with _runs_lock:
            _runs[run_id].update(
                state="done",
                status="failed",
                reason=f"Unexpected backend error: {exc}",
                finished_at=_now(),
            )
    finally:
        _cancel_events.pop(run_id, None)
    _write_status(run_id, run_dir)


@app.post("/api/rtl_generate")
def post_rtl_generate(body: RtlGenerateRequest):
    """RTL generation (`rtl` deliverable): one headless RTL_Coder run in a
    background thread - returns {run_id} immediately; poll GET /api/runs/{id}
    like every other run (records carry deliverable "rtl" and cost/duration).
    The run verdict is SERVER-ENFORCED: the backend re-runs iverilog -g2012 +
    vvp itself per block (and the top) and downgrades any false 'verified';
    scope=controller supports the 'partial' verdict (some blocks verified,
    still filed). Timeouts: 1800 s (block) / 5400 s (controller)."""
    try:
        iface = interfaces_mod.normalize_interface(body.interface)
        architecture = arch_chat_mod.normalize_architecture(body.digital_architecture)
        rtl_generate_mod.validate_rtl_request(
            body.scope, body.phy_type, architecture, body.block_id,
            body.block_fields, iface, body.library, body.cell,
        )
        # Unknown spec_doc_ids are a 422 too (resolved again at run time).
        spec_docs_mod.resolve_docs_for_generation(body.phy_type, body.spec_doc_ids or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    run_id = uuid.uuid4().hex[:12]
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    spec_dict = {
        "scope": body.scope,
        "phy_type": body.phy_type,
        "digital_architecture": architecture,
        "block_id": body.block_id,
        "block_fields": body.block_fields or {},
        "interface": iface,
        "spec_doc_ids": body.spec_doc_ids or [],
        "library": body.library,
        "cell": body.cell,
        "deliverable": "rtl",
    }
    (run_dir / "spec.json").write_text(json.dumps(spec_dict, indent=2))

    label = (
        f"RTL - {body.block_id}" if body.scope == "block"
        else f"RTL - {body.phy_type} controller"
    )
    with _runs_lock:
        _runs[run_id] = {
            "run_id": run_id,
            "label": label,
            "spec": spec_dict,
            "deliverable": "rtl",
            "light_run": False,
            "warnings": [],
            "state": "running",
            "status": None,
            "created_at": _now(),
            "finished_at": None,
        }
    _write_status(run_id, run_dir)

    cancel_event = threading.Event()
    _cancel_events[run_id] = cancel_event
    thread = threading.Thread(
        target=_execute_rtl, args=(run_id, run_dir, spec_dict, cancel_event), daemon=True
    )
    thread.start()

    return {"run_id": run_id}


@app.get("/api/topologies")
def get_topologies():
    """Canonical topology dropdown options, grouped for <optgroup> rendering.
    Single source of truth shared with build_prompt()'s generic-topology
    branch - see backend/topologies.py. Each option also carries a "models"
    applicability map (cycle 2); "custom_models" is the same map for the
    free-text custom topology, which has no option entry of its own."""
    return {
        "groups": TOPOLOGY_GROUPS,
        "custom_value": CUSTOM_TOPOLOGY_VALUE,
        "custom_models": MODEL_APPLICABILITY["custom"],
    }


@app.get("/api/libraries")
def get_libraries():
    """Hierarchical design libraries: each library is a directory under
    libraries/, each cell a subdirectory of views (schematic/symbol/netlist/
    testbench/image/behavioral models/measurements) - the same directory
    convention xschem browses via XSCHEM_LIBRARY_PATH. Used by the spec
    form's library picker."""
    return {"libraries": list_libraries()}


class LibraryCreate(BaseModel):
    name: str = Field(description="Library name: letters/digits/_/-, max 64 chars")


@app.post("/api/libraries")
def post_library(body: LibraryCreate):
    try:
        return create_library(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/libraries/{library}/{cell}/file/{name}")
def get_library_file(library: str, cell: str, name: str):
    """Serve one view file of a library cell (same traversal guards as the
    run-file endpoint). The library is located across the current + previous
    libraries roots, so cells filed before a working-dir change stay
    viewable."""
    for part in (library, cell, name):
        if "/" in part or "\\" in part or part in (".", ".."):
            raise HTTPException(status_code=400, detail="invalid path component")
    for root in all_libraries_roots():
        path = (root / library / cell / name).resolve()
        if root.resolve() not in path.parents:
            raise HTTPException(status_code=400, detail="invalid path")
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404, detail="file not found")


@app.get("/api/toolchain")
def get_toolchain():
    """Which model-verification tools are actually installed right now
    (openvaf for Verilog-A, iverilog/vvp for Verilog+RNM), plus the one-shot
    Icarus real-port probe result. The form uses this to warn inline that a
    requested model will be generated but not verified; the values are also
    baked into the Circuit_Builder prompt contract at run time."""
    return toolchain_status()


class SettingsUpdate(BaseModel):
    working_dir: str = Field(
        description="Directory the tool creates its data under (libraries/, runs/, "
        "research_runs/ subtrees) - absolute path or ~-prefixed; created if missing"
    )


@app.get("/api/settings")
def get_settings():
    """Current tool settings: the working dir (with a using-default flag),
    the derived data sub-paths, and where any previous-location data lives."""
    return settings_payload()


@app.put("/api/settings")
def put_settings(body: SettingsUpdate):
    """Change the working dir. Validated (absolute after ~-expansion,
    created if missing, writable, not inside the tool's source trees) and
    applied at the next request - no restart. Existing data is NOT moved:
    the old location is remembered and stays on the read path; then the
    run/research registries are rescanned so anything already at the new
    location shows up immediately."""
    try:
        payload = set_working_dir(body.working_dir)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _load_runs_from_disk()
    _load_research_runs_from_disk()
    return payload


@app.post("/api/runs")
def create_run(spec: RunSpec):
    run_id = uuid.uuid4().hex[:12]
    # New runs always land under the CURRENT working dir's runs/ root,
    # resolved from settings at request time (no restart after a change).
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    spec_dict = spec.model_dump()
    (run_dir / "spec.json").write_text(json.dumps(spec_dict, indent=2))

    with _runs_lock:
        _runs[run_id] = {
            "run_id": run_id,
            "label": spec.label,
            "spec": spec_dict,
            # Single-deliverable mode (top-level, not just buried in spec) so
            # the UI can label runs by what they produced; light_run marks
            # the cheap/fast modes (symbol + model-only: no transistor-level
            # design, no SPICE verification suite).
            "deliverable": spec.deliverable,
            "light_run": spec.deliverable in LIGHT_DELIVERABLES,
            # Soft sky130-feasibility warnings active at submit time (see
            # _active_soft_warnings) - persisted so a result viewed later
            # still carries its "this spec was aggressive" context.
            "warnings": _active_soft_warnings(spec),
            "state": "running",  # "running" | "done"
            "status": None,  # "success" | "failed", set once done
            "created_at": _now(),
            "finished_at": None,
        }
    _write_status(run_id, run_dir)

    cancel_event = threading.Event()
    _cancel_events[run_id] = cancel_event
    thread = threading.Thread(target=_execute, args=(run_id, run_dir, spec_dict, cancel_event), daemon=True)
    thread.start()

    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    with _runs_lock:
        run = _runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run["state"] != "running":
            raise HTTPException(status_code=409, detail="run is not currently running")
    event = _cancel_events.get(run_id)
    if event is None:
        raise HTTPException(status_code=409, detail="run cannot be cancelled (no active process)")
    event.set()
    return {"status": "cancelling"}


@app.post("/api/runs/{run_id}/open-schematic")
def open_schematic(run_id: str):
    """Launch the xschem GUI (not headless) on this run's schematic, so the
    user can inspect/edit it interactively. Requires the backend process to
    have access to a real X11 display - uses the backend's own DISPLAY env
    var if set, else falls back to this machine's known desktop display."""
    with _runs_lock:
        run = _runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="run directory not found on disk")
    # "current_mirror.sch" fallback only makes sense for the one topology
    # with a fixed, known filename; every other topology's schematic name is
    # whatever Circuit_Builder chose, recorded in artifacts.schematic_file.
    default_name = "current_mirror.sch" if (run.get("spec") or {}).get("topology") == "nmos_current_mirror" else None
    schematic_name = (run.get("artifacts") or {}).get("schematic_file") or default_name
    if not schematic_name:
        raise HTTPException(status_code=404, detail="no schematic artifact recorded for this run")
    schematic_path = run_dir / schematic_name
    if not schematic_path.exists():
        raise HTTPException(status_code=404, detail=f"schematic file {schematic_name} not found for this run")

    env = os.environ.copy()
    env.setdefault("DISPLAY", FALLBACK_DISPLAY)
    log_path = run_dir / "xschem_gui.log"
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.Popen(
                ["xschem", schematic_name],
                cwd=str(run_dir),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=f"could not launch xschem: {exc}") from None

    # Give it a moment - if it's going to fail fast (e.g. no display, bad
    # file), it'll usually exit within a second or two; if it's still
    # running, assume a GUI window is opening.
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return {"status": "launched", "display": env["DISPLAY"]}
    tail = log_path.read_text()[-800:] if log_path.exists() else ""
    raise HTTPException(
        status_code=500,
        detail=f"xschem exited immediately (code {proc.returncode}) using DISPLAY={env['DISPLAY']}: {tail}",
    )


@app.get("/api/schematic/resolve/{topology}")
def resolve_topology_schematic(topology: str):
    """Best available schematic for a topology (cycle 4 sub-window): a filed
    library cell with that topology in its provenance beats the latest
    successful run's rendered PNG; neither -> found=false with the defined
    empty-state message. Also carries current X-display availability so the
    frontend can enable/disable its "Open in xschem" button - re-checked on
    every call because the desktop session can come and go independently of
    the backend."""
    try:
        validate_topology_name(topology)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with _runs_lock:
        runs = list(_runs.values())
    resolved = resolve_schematic(topology, runs)
    resolved["xschem"] = display_status()
    return resolved


class SchematicOpenRequest(BaseModel):
    topology: str = Field(description="Topology whose resolved schematic to open in xschem")


@app.post("/api/schematic/open")
def open_topology_schematic(body: SchematicOpenRequest):
    """Spawn interactive xschem on the topology's resolved schematic (same
    resolution as GET /api/schematic/resolve - re-resolved server-side so the
    client can't point this at arbitrary paths). Detached, one live window
    per schematic file; 409 when no X display is reachable at request time."""
    try:
        validate_topology_name(body.topology)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    with _runs_lock:
        runs = list(_runs.values())
    resolved = resolve_schematic(body.topology, runs)
    if not resolved.get("found"):
        raise HTTPException(status_code=404, detail=resolved.get("message"))
    try:
        return launch_for_resolved(resolved)
    except (DisplayUnavailable, ValueError) as exc:
        # No display / nothing xschem-openable for this block: a state the UI
        # explains, not a backend fault.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except LaunchFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None


@app.get("/api/runs")
def list_runs():
    with _runs_lock:
        # .get() throughout: _load_runs_from_disk is deliberately lenient
        # about partial status.json records, so the list must be too.
        items = sorted(_runs.values(), key=lambda r: r.get("created_at") or "", reverse=True)
        return [
            {
                "run_id": r.get("run_id"),
                "label": r.get("label"),
                "state": r.get("state"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "finished_at": r.get("finished_at"),
                # Spec context so the frontend's architecture diagram can mark
                # which PHY sub-blocks already have builds (and with what
                # chosen architecture) without fetching every run's detail.
                "topology": (r.get("spec") or {}).get("topology"),
                # Deliverable mode + cheap-run flag so run lists can label
                # e.g. "RNM model only (light run)" without a detail fetch.
                # Falls back into spec for pre-feature records.
                "deliverable": r.get("deliverable") or (r.get("spec") or {}).get("deliverable") or "full",
                "light_run": bool(r.get("light_run")),
                "phy_type": (r.get("spec") or {}).get("phy_type"),
                "selected_block": (r.get("spec") or {}).get("selected_block"),
                "chosen_architecture": (r.get("spec") or {}).get("chosen_architecture"),
                # Soft feasibility warnings persisted at submit time (cycle
                # 1.5 leftover): list consumers (e.g. anything summarizing
                # run history) get the same flags as the detail view.
                "warnings": r.get("warnings") or [],
            }
            for r in items
        ]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with _runs_lock:
        run = _runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return dict(run)


@app.get("/api/runs/{run_id}/file/{name}")
def get_run_file(run_id: str, name: str):
    run_dir = _find_run_dir(run_id)
    if run_dir is None:
        raise HTTPException(status_code=404, detail="run not found")
    # Guard against path traversal - only allow a bare filename within run_dir.
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = (run_dir / name).resolve()
    if run_dir.resolve() not in path.parents and path != run_dir.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path)
