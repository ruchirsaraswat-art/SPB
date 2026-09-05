"""Unit checks for backend/results.py (results sub-window).

Plain-python assert style (no pytest on this machine). Run with:
    backend/.venv/bin/python backend/tests/test_results.py

Mirrors tests/test_schematics.py's fixture style for the resolution-order
checks; the MEAS-parsing check reads a REAL sim log already on disk in this
repo (runs/9b8b7de8b992/rnm_sim.log) rather than a synthetic string, per this
feature's verification requirement.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import results as rs  # noqa: E402

CHECKS = 0
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


def make_run(tmp: Path, run_id: str, topology, *, status="success",
             created_at="2026-08-21T10:00:00+00:00", write_summary=True) -> dict:
    run_dir = tmp / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {"status": "success", "topology": topology or "legacy", "measured": {"gain_db": 12.3}}
    if write_summary:
        (run_dir / "summary.json").write_text(json.dumps(summary))
    spec = {"topology": topology} if topology is not None else {}
    return {
        "run_id": run_id, "state": "done", "status": status, "spec": spec,
        "summary": summary if write_summary else {}, "created_at": created_at,
        "label": f"run {run_id}",
    }


def make_cell(tmp: Path, lib: str, cell: str, topology: str, *,
              with_summary=True, filed_at="2026-08-21T12:00:00+00:00") -> Path:
    cell_dir = tmp / "libs" / lib / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    if with_summary:
        (cell_dir / "summary.json").write_text(json.dumps({"status": "success", "topology": topology}))
    (cell_dir / "provenance.json").write_text(json.dumps({
        "run_id": "deadbeef0000", "topology": topology, "filed_at": filed_at, "label": f"filed {cell}",
    }))
    return cell_dir


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    libs = tmp / "libs"
    libs.mkdir()

    # --- 1-2: empty state when nothing matches ------------------------------
    r = rs.resolve_results("nonexistent_topology_xyz", [], libs_dir=libs, runs_dir=tmp / "runs")
    ok("empty state: found is False", r["found"] is False)
    ok("empty state: carries the defined message", r["message"] == rs.EMPTY_STATE_MESSAGE)

    # --- 3-6: run candidate picked when no library cell exists --------------
    run_a = make_run(tmp, "runA", "ctle", created_at="2026-01-01T00:00:00+00:00")
    run_b = make_run(tmp, "runB", "ctle", created_at="2026-06-01T00:00:00+00:00")  # newer
    r = rs.resolve_results("ctle", [run_a, run_b], libs_dir=libs, runs_dir=tmp / "runs")
    ok("run candidate: found", r["found"] is True)
    ok("run candidate: source is 'run'", r["source"] == "run")
    ok("run candidate: NEWEST successful run wins", r["run_id"] == "runB", r["run_id"])
    ok("run candidate: file_url_base points at the run", r["file_url_base"] == "/api/runs/runB/file/")

    # --- 7-8: a failed run is never picked -----------------------------------
    run_failed = make_run(tmp, "runFail", "ctle", status="failed", created_at="2026-12-01T00:00:00+00:00")
    r = rs.resolve_results("ctle", [run_a, run_b, run_failed], libs_dir=libs, runs_dir=tmp / "runs")
    ok("a failed run (even if newest) is not picked", r["run_id"] == "runB", r["run_id"])

    # --- 9-11: a filed library cell beats any run ---------------------------
    make_cell(tmp, "cal_runs", "ctle_cell", "ctle")
    r = rs.resolve_results("ctle", [run_a, run_b, run_failed], libs_dir=libs, runs_dir=tmp / "runs")
    ok("library candidate wins over runs", r["source"] == "library")
    ok("library candidate: correct library/cell", (r["library"], r["cell"]) == ("cal_runs", "ctle_cell"))
    ok(
        "library candidate: waveform_url_base points at the cell",
        r["waveform_url_base"] == "/api/libraries/cal_runs/ctle_cell/waveform/",
    )

    # --- 12: legacy runs (no topology field) resolve as nmos_current_mirror --
    legacy = make_run(tmp, "legacyRun", None, created_at="2026-01-01T00:00:00+00:00")
    r = rs.resolve_results("nmos_current_mirror", [legacy], libs_dir=libs, runs_dir=tmp / "runs")
    ok("legacy (topology-less) run resolves under nmos_current_mirror", r.get("run_id") == "legacyRun")

    # --- 13: a library cell provenance'd for a topology but with no
    #         summary.json filed yet is skipped (falls through to a run) ----
    cell_dir = make_cell(tmp, "cal_runs", "nosum", "no_summary_topology", with_summary=False)
    r = rs.resolve_results("no_summary_topology", [], libs_dir=libs, runs_dir=tmp / "runs")
    ok("library cell with no summary.json is not a candidate", r["found"] is False)

    # --- 14-15: waveform availability reflects real files on disk -----------
    r = rs.resolve_results("ctle", [run_a, run_b, run_failed], libs_dir=libs, runs_dir=tmp / "runs")
    ok("no .raw on disk -> waveform not available", r["waveform"]["available"] is False)
    (Path(r["dir"]) / "ctle_tb.raw").write_bytes(b"fake raw bytes")
    r = rs.resolve_results("ctle", [run_a, run_b, run_failed], libs_dir=libs, runs_dir=tmp / "runs")
    ok(
        "adding a .raw file makes waveform available",
        r["waveform"]["available"] is True and r["waveform"]["files"][0]["name"] == "ctle_tb.raw",
        r["waveform"],
    )

    # --- 16-17: artifacts collected only for files that actually exist ------
    run_c = make_run(tmp, "runC", "ctle2", created_at="2026-01-01T00:00:00+00:00")
    run_c["summary"]["netlist_file"] = "ctle2.spice"  # claimed but never written to disk
    run_c["summary"]["sim_log_file"] = "sim.log"
    (tmp / "runs" / "runC" / "sim.log").write_text("MEAS gain_db=12.3\n")
    r = rs.resolve_results("ctle2", [run_c], libs_dir=libs, runs_dir=tmp / "runs")
    ok("artifact present on disk is linked", r["artifacts"].get("sim_log_file") == "sim.log")
    ok("artifact claimed but missing on disk is NOT linked", "netlist_file" not in r["artifacts"])

# --- MEAS/RESULT_ log-scraping unit checks ----------------------------------
sample_text = """  ok   iout_nominal_A = 1.9999e-05  (window 1.99e-05 .. 2.01e-05)
MEAS iout_ua=19.999045
MEAS ratio=1.999905
MEAS settle_frac_1tau=0.3679 settle_frac_5tau_pct=0.6693
RNM_TB_PASS
"""
parsed = rs.parse_sim_log(sample_text)
ok(
    "MEAS single key=value parsed",
    {"name": "iout_ua", "value": 19.999045} in parsed["entries"],
    parsed["entries"],
)
ok(
    "MEAS line with TWO key=value pairs parses both",
    {"name": "settle_frac_1tau", "value": 0.3679} in parsed["entries"]
    and {"name": "settle_frac_5tau_pct", "value": 0.6693} in parsed["entries"],
    parsed["entries"],
)
ok("PASS token detected", parsed["tokens"] == ["RNM_TB_PASS"], parsed["tokens"])

result_style_text = "RESULT_IREF_A = 1E-05\nRESULT_VDS_M2_V = 0.9\nNote: Simulation executed from .control section\n"
parsed2 = rs.parse_sim_log(result_style_text)
ok(
    "older 'RESULT_* = value' assignment style also parsed",
    {"name": "RESULT_IREF_A", "value": 1e-05} in parsed2["entries"]
    and {"name": "RESULT_VDS_M2_V", "value": 0.9} in parsed2["entries"],
    parsed2["entries"],
)
ok("no PASS/FAIL token in a log that has none", parsed2["tokens"] == [])

# --- real on-disk log (not synthetic) ---------------------------------------
real_log = REPO_ROOT / "runs" / "9b8b7de8b992" / "rnm_sim.log"
if real_log.is_file():
    parsed3 = rs.parse_sim_log(real_log.read_text())
    ok(
        f"real log {real_log.relative_to(REPO_ROOT)}: MEAS entries parsed",
        any(e["name"] == "iout_ua" and abs(e["value"] - 19.999045) < 1e-6 for e in parsed3["entries"]),
        parsed3["entries"],
    )
    ok("real log: RNM_TB_PASS token found", "RNM_TB_PASS" in parsed3["tokens"])
else:
    print(f"skip  real-log check: {real_log} not present in this checkout")

print(f"\nall {CHECKS} checks passed")
