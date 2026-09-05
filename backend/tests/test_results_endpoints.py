"""Integration checks for the results sub-window's HTTP surface:
GET /api/results/resolve/{topology}, GET /api/runs/{run_id}/waveform/{name},
GET /api/libraries/{library}/{cell}/waveform/{name}.

Isolated-settings pattern matching tests/test_deliverables.py (a scratch
working dir, never the real repo tree) so fixture runs/cells never touch
real data. Plain-python assert style; run with:
    backend/.venv/bin/python backend/tests/test_results_endpoints.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.TemporaryDirectory()
_settings_file = Path(_TMP.name) / "settings.json"
_workdir = Path(_TMP.name) / "wd"
_workdir.mkdir()
_settings_file.write_text(json.dumps({"working_dir": str(_workdir)}))
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(_settings_file)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


client = TestClient(main.app)

# --- fixture: one successful run with a real sim log + a real ascii raw ----
run_id = "fixtureRun01"
run_dir = _workdir / "runs" / run_id
run_dir.mkdir(parents=True)
summary = {
    "status": "success", "topology": "ctle_endpoint_test", "corner": "tt",
    "vdd_v": 1.8, "temp_c": 27.0,
    "target_spec": {"gain_db": 10.0}, "measured": {"gain_db": 9.9},
    "sim_log_file": "sim.log", "waveform_file": "ctle_tb.raw",
}
(run_dir / "summary.json").write_text(json.dumps(summary))
(run_dir / "sim.log").write_text("MEAS gain_db=9.9\nCTLE_TB_PASS\n")
(run_dir / "ctle_tb.raw").write_text(
    "Flags: real\nNo. Variables: 2\nNo. Points: 2\nVariables:\n\t0\ttime\ttime\n\t1\tv(out)\tvoltage\n"
    "Values:\n 0\t0.0\n\t0.0\n\n 1\t1e-09\n\t0.5\n"
)
status = {
    "run_id": run_id, "spec": {"topology": "ctle_endpoint_test"},
    "state": "done", "status": "success", "summary": summary,
    "created_at": "2026-01-01T00:00:00+00:00", "label": "fixture run",
}
(run_dir / "status.json").write_text(json.dumps(status))

# main._runs is normally populated from disk by _load_runs_from_disk() at
# startup; called explicitly here (module-level statement, not the FastAPI
# lifespan) since these fixture run dirs are dropped onto disk directly
# rather than created through POST /api/runs.
main._load_runs_from_disk()
r = client.get(f"/api/results/resolve/ctle_endpoint_test")
ok("resolve: 200", r.status_code == 200, r.text)
body = r.json()
ok("resolve: found", body.get("found") is True, body)
ok("resolve: resolved to the fixture run", body.get("run_id") == run_id, body)
ok("resolve: MEAS entry parsed from sim.log", any(
    e["name"] == "gain_db" and abs(e["value"] - 9.9) < 1e-9
    for log in body["logs"] for e in log["entries"]
), body["logs"])
ok("resolve: pass token surfaced", any("CTLE_TB_PASS" in log["tokens"] for log in body["logs"]))
ok("resolve: waveform available (a .raw sits in the run dir)", body["waveform"]["available"] is True)
ok("resolve: waveform file name reported", body["waveform"]["files"][0]["name"] == "ctle_tb.raw")

# --- empty state for a topology with nothing filed --------------------------
r = client.get("/api/results/resolve/nonexistent_topology_for_test")
ok("resolve: empty state is still 200 (found:false, not an error)", r.status_code == 200)
ok("resolve: empty state message present", r.json().get("found") is False)

# --- bad topology name rejected ----------------------------------------------
r = client.get("/api/results/resolve/../../etc")
ok("resolve: path-traversal-shaped topology rejected", r.status_code in (404, 422), r.status_code)

# --- waveform data endpoint (run-scoped) ------------------------------------
r = client.get(f"/api/runs/{run_id}/waveform/ctle_tb.raw")
ok("waveform (run): 200", r.status_code == 200, r.text)
wdata = r.json()
ok("waveform (run): signals parsed", wdata["signals"] == ["time", "v(out)"], wdata)
ok("waveform (run): values parsed", wdata["data"]["v(out)"] == [0.0, 0.5], wdata["data"])

r = client.get(f"/api/runs/{run_id}/waveform/nope.raw")
ok("waveform (run): missing file -> 404", r.status_code == 404)

r = client.get(f"/api/runs/{run_id}/waveform/..%2f..%2fsecrets")
ok("waveform (run): path traversal rejected", r.status_code in (400, 404), r.status_code)

r = client.get("/api/runs/does-not-exist/waveform/ctle_tb.raw")
ok("waveform (run): unknown run -> 404", r.status_code == 404)

# --- waveform data endpoint (library-cell-scoped) ---------------------------
cell_dir = _workdir / "libraries" / "cal_runs" / "ctle_fixture"
cell_dir.mkdir(parents=True)
(cell_dir / "provenance.json").write_text(json.dumps({
    "run_id": run_id, "topology": "ctle_endpoint_test", "filed_at": "2026-02-01T00:00:00+00:00",
}))
(cell_dir / "summary.json").write_text(json.dumps(summary))
(cell_dir / "ctle_tb.raw").write_text((run_dir / "ctle_tb.raw").read_text())

r = client.get("/api/libraries/cal_runs/ctle_fixture/waveform/ctle_tb.raw")
ok("waveform (library): 200", r.status_code == 200, r.text)
ok("waveform (library): values parsed", r.json()["data"]["v(out)"] == [0.0, 0.5])

# A filed cell now exists for this topology -> it beats the run candidate.
r = client.get("/api/results/resolve/ctle_endpoint_test")
ok("resolve: filed library cell now wins over the run", r.json().get("source") == "library", r.json())

print(f"\nall {CHECKS} checks passed")
