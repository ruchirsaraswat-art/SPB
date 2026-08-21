"""Unit checks for backend/schematics.py (cycle 4 schematic sub-window).

Plain-python assert style (no pytest on this machine). Run with:
    backend/.venv/bin/python backend/tests/test_schematics.py
Exits non-zero on the first failure; prints a per-check pass line otherwise.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schematics as sm  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


def make_run(tmp: Path, run_id: str, topology, png: str | None, *,
             status="success", created_at="2026-08-21T10:00:00+00:00",
             write_png=True) -> dict:
    """A run-registry entry + on-disk run dir like main.py maintains."""
    run_dir = tmp / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if png and write_png:
        (run_dir / png).write_bytes(b"\x89PNG fake")
    spec = {"topology": topology} if topology is not None else {}
    return {
        "run_id": run_id,
        "state": "done",
        "status": status,
        "spec": spec,
        "summary": {"schematic_png": png, "schematic_file": png and png.replace(".png", ".sch")},
        "created_at": created_at,
        "label": f"run {run_id}",
    }


def make_cell(tmp: Path, lib: str, cell: str, topology: str, *,
              with_png=True, with_sch=True,
              filed_at="2026-08-21T12:00:00+00:00") -> Path:
    cell_dir = tmp / "libs" / lib / cell
    cell_dir.mkdir(parents=True, exist_ok=True)
    if with_png:
        (cell_dir / f"{cell}.png").write_bytes(b"\x89PNG fake")
    if with_sch:
        (cell_dir / f"{cell}.sch").write_text("v {xschem}")
        (cell_dir / f"{cell}_tb.sch").write_text("v {xschem}")  # must never be picked
    (cell_dir / "provenance.json").write_text(json.dumps({
        "run_id": "deadbeef0000", "topology": topology, "filed_at": filed_at,
        "label": f"filed {cell}",
    }))
    return cell_dir


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    libs = tmp / "libs"
    runs_dir = tmp / "runs"
    libs.mkdir()
    runs_dir.mkdir()

    def resolve(topology, runs):
        return sm.resolve_schematic(topology, runs, libs_dir=libs, runs_dir=runs_dir)

    # --- empty state --------------------------------------------------------
    r = resolve("pll", [])
    ok("unknown block -> found=false", r["found"] is False)
    ok("empty state carries the defined message",
       r["message"] == "No schematic yet — submit a spec for this block first.", r["message"])

    # --- run fallback -------------------------------------------------------
    old = make_run(tmp, "aaa111", "ctle", "ctle.png", created_at="2026-08-20T09:00:00+00:00")
    new = make_run(tmp, "bbb222", "ctle", "ctle_v2.png", created_at="2026-08-21T09:00:00+00:00")
    failed = make_run(tmp, "ccc333", "ctle", "x.png", status="failed",
                      created_at="2026-08-22T09:00:00+00:00")
    r = resolve("ctle", [old, new, failed])
    ok("no library cell -> latest successful run wins",
       r["found"] and r["source"] == "run" and r["run_id"] == "bbb222", str(r))
    ok("run png_url points at the run file endpoint",
       r["png_url"] == "/api/runs/bbb222/file/ctle_v2.png", r.get("png_url"))

    # failed runs never resolve, even when newest
    r = resolve("ctle", [old, failed])
    ok("failed run skipped even when newest", r["run_id"] == "aaa111", str(r))

    # a successful run whose PNG vanished from disk is skipped
    gone = make_run(tmp, "ddd444", "ctle", "gone.png",
                    created_at="2026-08-23T09:00:00+00:00", write_png=False)
    r = resolve("ctle", [old, gone])
    ok("run with missing PNG file on disk skipped", r["run_id"] == "aaa111", str(r))

    # a successful run with no schematic_png recorded is skipped
    nopng = make_run(tmp, "eee555", "ctle", None, created_at="2026-08-23T09:00:00+00:00")
    r = resolve("ctle", [old, nopng])
    ok("run without recorded PNG skipped", r["run_id"] == "aaa111", str(r))

    # --- legacy runs (pre-topology spec) ------------------------------------
    legacy = make_run(tmp, "fff666", None, "current_mirror.png")
    r = resolve("nmos_current_mirror", [legacy])
    ok("legacy run (no spec.topology) matches nmos_current_mirror",
       r["found"] and r["run_id"] == "fff666", str(r))

    # --- library preference -------------------------------------------------
    make_cell(tmp, "cal_runs", "ctle_cell", "ctle")
    r = resolve("ctle", [old, new])
    ok("filed library cell preferred over newer successful run",
       r["source"] == "library" and r["library"] == "cal_runs" and r["cell"] == "ctle_cell", str(r))
    ok("library png_url points at the library file endpoint",
       r["png_url"] == "/api/libraries/cal_runs/ctle_cell/file/ctle_cell.png", r.get("png_url"))
    ok("library sch view is the cell schematic, not the testbench",
       r["sch"] == "ctle_cell.sch", r.get("sch"))

    # newer filing wins between two cells of the same topology
    make_cell(tmp, "serdes_rx", "ctle_newer", "ctle", filed_at="2026-08-22T12:00:00+00:00")
    r = resolve("ctle", [])
    ok("most recently filed cell wins", r["cell"] == "ctle_newer", str(r))

    # a PNG-less cell falls through to the run fallback
    make_cell(tmp, "cal_runs", "pll_cell", "pll", with_png=False)
    pll_run = make_run(tmp, "abc789", "pll", "pll.png")
    r = resolve("pll", [pll_run])
    ok("cell without PNG view falls back to run", r["source"] == "run" and r["run_id"] == "abc789", str(r))

    # --- topology name validation -------------------------------------------
    for bad in ("", "../etc", "a/b", "x" * 65):
        try:
            resolve(bad, [])
            ok(f"bad topology name {bad!r} rejected", False)
        except ValueError:
            pass
    ok("bad topology names rejected", True)

# --- display guard ----------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    x11 = Path(td)

    s = sm.display_status(environ={"DISPLAY": ":5"}, x11_dir=x11)
    ok("DISPLAY set but socket missing and no fallback -> unavailable",
       s["available"] is False and "reason" in s, str(s))

    (x11 / "X5").touch()
    s = sm.display_status(environ={"DISPLAY": ":5"}, x11_dir=x11)
    ok("DISPLAY set with live socket -> available on that display",
       s["available"] is True and s["display"] == ":5", str(s))

    s = sm.display_status(environ={}, x11_dir=x11)
    ok("no DISPLAY, fallback socket missing -> unavailable", s["available"] is False, str(s))

    (x11 / "X2").touch()  # fallback desktop display :2
    s = sm.display_status(environ={}, x11_dir=x11)
    ok("no DISPLAY, fallback socket present -> fallback used",
       s["available"] is True and s["display"] == sm.FALLBACK_DISPLAY, str(s))

    s = sm.display_status(environ={"DISPLAY": ":9"}, x11_dir=x11)
    ok("dead DISPLAY falls back to live desktop display",
       s["available"] is True and s["display"] == sm.FALLBACK_DISPLAY, str(s))

    s = sm.display_status(environ={"DISPLAY": "otherhost:0"}, x11_dir=x11)
    ok("non-local display string trusted", s["available"] is True and s["display"] == "otherhost:0", str(s))

print(f"\nall {CHECKS} checks passed")
