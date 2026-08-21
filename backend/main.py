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

from circuit_builder import run_circuit_builder
from circuit_researcher import run_circuit_researcher
from libraries import (
    create_library,
    file_run_into_library,
    list_libraries,
    validate_library_name,
)
from settings import (
    all_libraries_roots,
    all_research_roots,
    all_runs_roots,
    research_root,
    runs_root,
    set_working_dir,
    settings_payload,
)
from models_contract import (
    MODEL_APPLICABILITY,
    toolchain_status,
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

    # Virtuoso-style design library this block is filed into on success (user
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
        _validate_common_spec(self)
        validate_models_request(self.topology, self.models)
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
        result = run_circuit_builder(spec, run_dir, cancel_event=cancel_event)
        # File a successful run into its chosen design library (Virtuoso-style
        # lib/cell/view directory tree browsable from xschem). Failures here
        # (disk trouble, bad name that slipped past validation) are recorded
        # on the run instead of being raised - the build itself succeeded and
        # its run dir is intact, so don't report the whole run as failed.
        library_filed = None
        library_error = None
        if result.status == "success" and spec.get("library"):
            try:
                library_filed = file_run_into_library(
                    spec["library"], run_id, run_dir, spec, result.summary
                )
            except (ValueError, OSError) as exc:
                library_error = f"could not file result into library '{spec['library']}': {exc}"
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
    """Virtuoso-style design libraries: each library is a directory under
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
        items = sorted(_runs.values(), key=lambda r: r["created_at"], reverse=True)
        return [
            {
                "run_id": r["run_id"],
                "label": r["label"],
                "state": r["state"],
                "status": r.get("status"),
                "created_at": r["created_at"],
                "finished_at": r.get("finished_at"),
                # Spec context so the frontend's architecture diagram can mark
                # which PHY sub-blocks already have builds (and with what
                # chosen architecture) without fetching every run's detail.
                "topology": (r.get("spec") or {}).get("topology"),
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
