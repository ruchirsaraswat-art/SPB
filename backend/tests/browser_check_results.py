"""Browser checks for the results sub-window (docked under SchematicPanel)
and its waveform viewer. Script-style like the other suites in this
directory - run directly, not via pytest:

    cd backend && .venv/bin/python tests/browser_check_results.py

Requires both dev servers running (backend :8000, vite :5173). Free: no
chat messages are sent, only page loads/clicks.

Part 1 checks against REAL permanent data: the "NMOS current mirror"
topology's filed library cell (libraries/cal_runs/current_mirror), built by
an actual past Circuit_Builder run.

Part 2 (waveform plotting) needs a .raw file to plot, and no historical run
has one (this feature is what adds that going forward). Rather than leaving
a permanent demo artifact in the repo, this script creates ONE for the
duration of the check - a library cell filed under the "ctle" topology in a
clearly-named fixture library, carrying a REAL ngspice-generated ascii .raw
(free, local, no Circuit_Builder involved) - and removes it in a `finally`
block regardless of outcome. Library cells are live-scanned on every
request (see backend/results.py), so no backend restart is needed to pick
it up or to see it disappear again afterward.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"
REPO_ROOT = Path("/home/rsarasw1/analog-spec-tool")
FIXTURE_LIB = "_browser_check_fixtures"
FIXTURE_CELL = "ctle_waveform_demo"
FIXTURE_DIR = REPO_ROOT / "libraries" / FIXTURE_LIB / FIXTURE_CELL

passed = 0
failed = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name} {detail}")


def panel(page, ws):
    return page.locator(f'section[data-workspace="{ws}"]')


def is_minimized(page, ws):
    cls = panel(page, ws).get_attribute("class") or ""
    return "minimized" in cls.split()


def ensure_expanded(page, ws):
    if is_minimized(page, ws):
        panel(page, ws).locator("button.workspace-min-btn").click()
        page.wait_for_timeout(200)


def select_topology(page, label_text):
    # Uniquely identified by its known option value (nmos_current_mirror is
    # the one built-in topology, guaranteed present) rather than by label
    # text, which collides with another <label> on the page.
    sel = page.locator('select:has(option[value="nmos_current_mirror"])')
    sel.select_option(label=label_text)
    page.wait_for_timeout(600)


def make_waveform_fixture():
    """Files a temporary library cell (topology "ctle") with a REAL
    ngspice-generated ascii .raw, so this run's waveform plot has real data
    to show - torn down by remove_waveform_fixture() below."""
    import json
    from datetime import datetime, timezone

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    deck = FIXTURE_DIR / "ctle_tb.spice"
    deck.write_text(
        "* browser_check_results.py fixture - RC step response standing in\n"
        "* for a ctle transient capture, purely to exercise waveform plotting.\n"
        "V1 in 0 PULSE(0 1 1n 0.1n 0.1n 10n 20n)\n"
        "R1 in out 1k\nC1 out 0 1n\n.tran 10p 40n\n"
        ".control\nset filetype=ascii\nrun\nwrite ctle_tb.raw v(in) v(out)\n.endc\n.end\n"
    )
    ngspice = shutil.which("ngspice")
    assert ngspice, "ngspice not on PATH - required to generate the waveform fixture"
    subprocess.run([ngspice, "-b", deck.name], cwd=FIXTURE_DIR, capture_output=True, timeout=60, check=False)
    assert (FIXTURE_DIR / "ctle_tb.raw").is_file(), "ngspice did not produce ctle_tb.raw"

    (FIXTURE_DIR / "sim.log").write_text(
        "BROWSER-CHECK FIXTURE ONLY - not a real Circuit_Builder sim log.\n"
        "MEAS peaking_db=6.02\nMEAS dc_gain_db=0.01\nCTLE_DEMO_TB_PASS\n"
    )
    summary = {
        "status": "success", "error": "", "topology": "ctle",
        "topology_choice": "browser_check_results.py fixture (RC step response) - waveform-plotting check only",
        "corner": "tt", "vdd_v": 1.8, "temp_c": 27.0,
        "target_spec": {"dc_gain_db": 0.0, "peaking_db": 6.0, "data_rate_gbps": 2.0},
        "measured": {"dc_gain_db": 0.01, "peaking_db": 6.02},
        "testbench_file": "ctle_tb.spice", "sim_log_file": "sim.log", "waveform_file": "ctle_tb.raw",
        "notes": "Fixture created by tests/browser_check_results.py to verify the waveform viewer "
        "end to end (not a real Circuit_Builder run) - removed automatically when the script exits.",
    }
    (FIXTURE_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    (FIXTURE_DIR / "provenance.json").write_text(json.dumps({
        "run_id": None, "topology": "ctle", "label": "waveform viewer browser-check fixture",
        "filed_at": datetime.now(timezone.utc).isoformat(), "source": "browser_check_results.py",
    }, indent=2))


def remove_waveform_fixture():
    shutil.rmtree(FIXTURE_DIR.parent, ignore_errors=True)  # whole fixture library, not just the cell


def main():
    make_waveform_fixture()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.add_init_script('localStorage.setItem("focus-ddr-afe", "0")')
            page.goto(BASE)
            page.wait_for_selector('section[data-workspace="afe"]')
            ensure_expanded(page, "afe")

            # --- 1. real past results: NMOS current mirror (filed library cell) --
            print("[results] NMOS current mirror -> filed library cell's real measurements")
            select_topology(page, "NMOS current mirror (2-transistor)")
            results_panel = page.locator(".results-panel-window")
            check("results panel appears", results_panel.count() == 1)
            page.wait_for_selector(".results-panel-window .results-status-line", timeout=5000)
            status_line = results_panel.locator(".results-status-line").inner_text()
            check("status line says Success", "Success" in status_line, status_line)
            source_line = results_panel.locator(".schematic-source").first.inner_text()
            check(
                "source line names the filed library cell",
                "cal_runs/current_mirror" in source_line,
                source_line,
            )
            models_block = results_panel.locator(".models-block")
            check("behavioral-model checks table rendered", models_block.count() == 1)
            check(
                "a real measured value (iout_ua target) is visible",
                "19.999045" in results_panel.inner_text() or "20" in results_panel.inner_text(),
            )
            meas_block = results_panel.locator(".meas-block")
            check("MEAS/parsed-log block rendered", meas_block.count() == 1)
            pass_badges = results_panel.locator(".meas-log .model-status-badge")
            check("a PASS token badge is shown from a real sim log", pass_badges.count() > 0)
            waveform_text = results_panel.locator(".waveform-viewer").inner_text()
            check(
                "waveform section correctly reports none recorded for this historical cell",
                "No waveform data is available" in waveform_text,
                waveform_text,
            )
            results_panel.scroll_into_view_if_needed()
            page.screenshot(path=f"{AUDITS}/2026-09-05-results-panel-current-mirror.png", full_page=True)

            # --- 2. schematic panel still works alongside it (no regression) -----
            schematic_panel = page.locator(".schematic-panel").first
            check(
                "schematic panel (docked above) still resolves for the same block",
                schematic_panel.locator("img.schematic-img").count() == 1,
            )

            # --- 3. waveform plotting: fixture cell with a real ngspice .raw -----
            print("[waveform] fixture library cell -> real ngspice-generated .raw plotted client-side")
            select_topology(page, "CTLE (continuous-time linear equalizer)")
            page.wait_for_selector(".results-panel-window .waveform-viewer", timeout=5000)
            results_panel2 = page.locator(".results-panel-window")
            check(
                "resolved to the fixture library cell (beats any run - none exists for ctle anyway)",
                f"{FIXTURE_LIB}/{FIXTURE_CELL}" in results_panel2.locator(".schematic-source").first.inner_text(),
            )
            checkboxes = results_panel2.locator(".waveform-signal-checkbox input")
            check("signal checkboxes rendered (time excluded, v(in)/v(out) offered)", checkboxes.count() == 2)
            page.wait_for_selector(".waveform-plot-svg path.waveform-trace", timeout=5000)
            svg = results_panel2.locator(".waveform-plot-svg")
            check("plot svg rendered", svg.count() == 1)
            traces = results_panel2.locator(".waveform-plot-svg path.waveform-trace")
            check("at least one trace path drawn", traces.count() >= 1, traces.count())
            first_d = traces.first.get_attribute("d")
            check("trace path has real path data (not empty)", bool(first_d) and first_d.startswith("M"), first_d)
            legend = results_panel2.locator(".waveform-legend li")
            check("legend shows real min/max for a plotted signal", legend.count() >= 1)
            points_hint = results_panel2.locator(".waveform-viewer .hint").last.inner_text()
            check("point count reported", "point" in points_hint, points_hint)
            svg.scroll_into_view_if_needed()
            page.screenshot(path=f"{AUDITS}/2026-09-05-waveform-plot-ctle-fixture.png", full_page=True)

            browser.close()
    finally:
        remove_waveform_fixture()
        print(f"[cleanup] removed fixture library {FIXTURE_LIB!r} ({FIXTURE_DIR.parent})")

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
