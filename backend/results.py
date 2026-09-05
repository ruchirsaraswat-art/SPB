"""Results sub-window support: "the best results we have for this topology"
(mirrors backend/schematics.py's resolution, one level down - measurements
instead of a rendered view).

Resolution (identical priority order to resolve_schematic, just without the
PNG requirement):
  1. A cell filed in a design library whose provenance records this topology
     and whose directory has a `summary.json` - most recently filed wins.
  2. Otherwise the most recent SUCCESSFUL run for this topology whose run dir
     still has a `summary.json` on disk. Runs from before the topology field
     existed are treated as the built-in "nmos_current_mirror" flow, same as
     schematics.py.
  3. Otherwise a defined empty state - the frontend shows it verbatim.

On top of the resolved summary.json, this module also best-effort-parses
whatever sim logs live alongside it (`MEAS name=value` lines - this project's
own convention, see rnm_sim.log/arch_sim.log - plus older `NAME = value`
assignment lines some runs used, e.g. sim.log's `RESULT_IREF_A = 1E-05`, and
any `..._PASS`/`..._FAIL` watchdog tokens), and lists any `.raw` waveform
files present (added going forward by circuit_builder.py's prompt contract -
see rawfile.py for the actual parser; historical runs simply have none).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from settings import all_libraries_roots, all_runs_roots
from schematics import (  # reuse, don't fork: same legacy-topology rule, same path-safety gate
    _LEGACY_DEFAULT_TOPOLOGY,
    validate_topology_name,
)

EMPTY_STATE_MESSAGE = "No results yet — submit a spec for this block first."

# Same key list backend/circuit_builder.py's evaluate_summary() checks
# claimed artifacts against - kept as a separate literal (not imported) so
# this read-only module has no dependency on the run-execution module,
# same posture schematics.py already takes toward circuit_builder (it only
# imports the one xschemrc helper it needs).
_ARTIFACT_KEYS = (
    "schematic_png", "netlist_file", "testbench_file", "testbench_sch_file",
    "sim_log_file", "schematic_file", "symbol_file", "dc_log_file", "top_file",
    "waveform_file",
)

# Log files worth scraping for measurements: everything named *_sim.log or
# exactly sim.log, excluding the session transcript / GUI / capture logs that
# happen to also end in .log.
_SIM_LOG_EXCLUDE = {"session.log", "capture.log", "xschem_gui.log"}


def _is_sim_log(name: str) -> bool:
    if name in _SIM_LOG_EXCLUDE or name.endswith(".server.log"):
        return False
    return name == "sim.log" or name.endswith("_sim.log") or name.endswith(".log")


# --- sim log scraping --------------------------------------------------------

_MEAS_LINE_RE = re.compile(r"^\s*MEAS\s+(.+)$")
_ASSIGN_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*([-+0-9][-+0-9.eE]*)")
_WHOLE_LINE_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*([-+0-9][-+0-9.eE]*)\s*$")
_PASS_FAIL_RE = re.compile(r"\b[A-Z][A-Z0-9_]*_(?:PASS|FAIL)\b")


def _to_number(s: str) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        return None


def parse_sim_log(text: str) -> dict[str, Any]:
    """Best-effort text scraping, not a real ngspice/Verilog parser: pulls out
    `MEAS name=value [name=value ...]` lines (this project's convention) and
    single full-line `name = value` assignments (older runs' `RESULT_*`
    style; plain ngspice `.meas` output looks the same), plus any
    `..._PASS`/`..._FAIL` tokens. A log in a format this doesn't recognize
    just yields no entries - the raw log is always still linked so a person
    can read it directly."""
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _MEAS_LINE_RE.match(line)
        if m:
            for km in _ASSIGN_TOKEN_RE.finditer(m.group(1)):
                entries.append({"name": km.group(1), "value": _to_number(km.group(2))})
            continue
        wm = _WHOLE_LINE_ASSIGN_RE.match(line)
        if wm:
            entries.append({"name": wm.group(1), "value": _to_number(wm.group(2))})
    tokens = list(dict.fromkeys(_PASS_FAIL_RE.findall(text)))
    return {"entries": entries, "tokens": tokens}


def _collect_logs(directory: Path) -> list[dict[str, Any]]:
    logs = []
    for f in sorted(directory.glob("*.log")):
        if not f.is_file() or not _is_sim_log(f.name):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        parsed = parse_sim_log(text)
        if parsed["entries"] or parsed["tokens"]:
            logs.append({"name": f.name, **parsed})
        else:
            logs.append({"name": f.name, "entries": [], "tokens": []})
    return logs


def _collect_artifacts(directory: Path, summary: dict[str, Any]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for key in _ARTIFACT_KEYS:
        fname = summary.get(key)
        if fname and (directory / fname).is_file():
            artifacts[key] = fname
    return artifacts


def _collect_waveforms(directory: Path) -> dict[str, Any]:
    files = [
        {"name": f.name, "size_bytes": f.stat().st_size}
        for f in sorted(directory.glob("*.raw"))
        if f.is_file()
    ]
    if not files:
        return {
            "available": False,
            "files": [],
            "reason": "No waveform data is available for this run — either it predates waveform "
            "capture being added, or its testbench only ran a single operating point (nothing to "
            "plot over time/frequency). Measurements and logs below still reflect what was "
            "simulated.",
        }
    return {"available": True, "files": files, "reason": None}


# --- resolution ---------------------------------------------------------------


def _library_candidate(topology: str, libs_dirs: list[Path]) -> Optional[dict[str, Any]]:
    """Same traversal as schematics.py's _library_candidate, just requiring a
    summary.json instead of a rendered PNG."""
    best: Optional[dict[str, Any]] = None
    for libs_dir in libs_dirs:
        if not libs_dir.is_dir():
            continue
        for prov_path in libs_dir.glob("*/*/provenance.json"):
            try:
                prov = json.loads(prov_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if prov.get("topology") != topology:
                continue
            cell_dir = prov_path.parent
            summary_path = cell_dir / "summary.json"
            if not summary_path.is_file():
                continue  # nothing measured filed for this cell yet
            try:
                summary = json.loads(summary_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            cell = cell_dir.name
            library = cell_dir.parent.name
            candidate = {
                "found": True,
                "source": "library",
                "library": library,
                "cell": cell,
                "dir": str(cell_dir),
                "run_id": prov.get("run_id"),
                "label": prov.get("label"),
                "chosen_architecture": prov.get("chosen_architecture"),
                "filed_at": prov.get("filed_at") or "",
                "summary": summary,
                "spec": {},
                "file_url_base": f"/api/libraries/{library}/{cell}/file/",
                "waveform_url_base": f"/api/libraries/{library}/{cell}/waveform/",
            }
            if best is None or candidate["filed_at"] > best["filed_at"]:
                best = candidate
    return best


def _find_run_dir(run_id: str, runs_dirs: list[Path]) -> Optional[Path]:
    for root in runs_dirs:
        candidate = root / run_id
        if candidate.is_dir():
            return candidate
    return None


def _run_candidate(
    topology: str, runs: Iterable[dict[str, Any]], runs_dirs: list[Path]
) -> Optional[dict[str, Any]]:
    best: Optional[dict[str, Any]] = None
    for r in runs:
        if r.get("state") != "done" or r.get("status") != "success":
            continue
        spec = r.get("spec") or {}
        if (spec.get("topology") or _LEGACY_DEFAULT_TOPOLOGY) != topology:
            continue
        run_id = r.get("run_id")
        summary = r.get("summary") or {}
        if not run_id or not summary:
            continue
        run_dir = _find_run_dir(run_id, runs_dirs)
        if run_dir is None or not (run_dir / "summary.json").is_file():
            continue
        candidate = {
            "found": True,
            "source": "run",
            "run_id": run_id,
            "dir": str(run_dir),
            "label": r.get("label"),
            "chosen_architecture": spec.get("chosen_architecture"),
            "created_at": r.get("created_at") or "",
            "summary": summary,
            "spec": spec,
            "raw_text": r.get("raw_text"),
            "library_filed": r.get("library_filed"),
            "library_error": r.get("library_error"),
            "warnings": r.get("warnings") or [],
            "file_url_base": f"/api/runs/{run_id}/file/",
            "waveform_url_base": f"/api/runs/{run_id}/waveform/",
        }
        if best is None or candidate["created_at"] > best["created_at"]:
            best = candidate
    return best


def resolve_results(
    topology: str,
    runs: Iterable[dict[str, Any]],
    libs_dir: Path | list[Path] | None = None,
    runs_dir: Path | list[Path] | None = None,
) -> dict[str, Any]:
    """Best available results for a topology: filed library cell first, then
    the latest successful run, else a defined empty state. Enriches whichever
    candidate is chosen with parsed sim-log measurements, computed artifact
    links, and waveform-file availability."""
    validate_topology_name(topology)
    libs_dirs = (
        all_libraries_roots() if libs_dir is None
        else ([libs_dir] if isinstance(libs_dir, Path) else list(libs_dir))
    )
    runs_dirs = (
        all_runs_roots() if runs_dir is None
        else ([runs_dir] if isinstance(runs_dir, Path) else list(runs_dir))
    )
    candidate = _library_candidate(topology, libs_dirs) or _run_candidate(
        topology, runs, runs_dirs
    )
    if candidate is None:
        return {"topology": topology, "found": False, "message": EMPTY_STATE_MESSAGE}

    directory = Path(candidate["dir"])
    summary = candidate["summary"]
    candidate["status"] = summary.get("status") or "success"
    candidate["artifacts"] = _collect_artifacts(directory, summary)
    candidate["logs"] = _collect_logs(directory)
    candidate["waveform"] = _collect_waveforms(directory)
    return {"topology": topology, **candidate}
