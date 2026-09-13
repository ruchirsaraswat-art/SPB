"""Browser check for the live layout-session (magic) UI (2026-09
layout-editor integration - see backend/layout.py and
frontend/src/LayoutPanel.jsx). Mirrors browser_check_xschem_resize.py's
structure/scope closely - same two resize levers, same real-session
start/stop, just for the layout panel instead of the schematic one. Run
with:

    cd backend && .venv/bin/python tests/browser_check_layout_resize.py

Requires the SINGLE-ORIGIN server at :8000 (see
browser_check_xschem_resize.py's docstring for why - the vendored noVNC
client can't be served through Vite's :5173 dev server). Starts/stops a
REAL interactive magic session (Xvfb + magic + x11vnc on PATH), so it
cleans up after itself in a `finally` block.

Exercises, through the real UI:
  - the create-a-new-layout empty state for a block that has a filed
    schematic cell but no layout yet (nmos_current_mirror /
    cal_runs/current_mirror - library/cell pre-filled and locked, mirroring
    the resolved schematic cell)
  - starting a live magic session at a chosen resolution
  - Lever A (view size, CSS-only/instant) + fullscreen toggle
  - Lever B (display resolution, destructive restart) on the running
    session
  - clean stop
"""

import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"

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


def ensure_expanded(page, ws):
    cls = panel(page, ws).get_attribute("class") or ""
    if "minimized" in cls.split():
        panel(page, ws).locator("button.workspace-min-btn").click()
        page.wait_for_timeout(200)


def select_topology(page, label_text):
    sel = page.locator('select:has(option[value="nmos_current_mirror"])')
    sel.select_option(label=label_text)
    page.wait_for_timeout(600)


# This suite deliberately drives the REAL filed cell cal_runs/current_mirror
# (that is the only cell with a schematic and no layout, which is the exact
# empty state under test). It therefore MUTATES a tracked file, so it
# snapshots provenance.json and the .mag's existence up front and restores
# both in the finally block - otherwise every run leaves the working tree
# dirty.
from pathlib import Path as _Path  # noqa: E402

# backend/ is this file's grandparent; it is not on sys.path when the
# suite is run as `python tests/browser_check_layout_resize.py`.
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import settings as _settings_mod  # noqa: E402

_CELL_DIR = _settings_mod.libraries_root() / "cal_runs" / "current_mirror"
_PROV = _CELL_DIR / "provenance.json"
_MAG = _CELL_DIR / "current_mirror.mag"


def _snapshot():
    return (_PROV.read_bytes() if _PROV.exists() else None, _MAG.exists())


def _restore(snap):
    prov_bytes, mag_existed = snap
    if prov_bytes is not None:
        _PROV.write_bytes(prov_bytes)
    if not mag_existed and _MAG.exists():
        _MAG.unlink()


def main():
    _snap = _snapshot()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.add_init_script('localStorage.setItem("focus-ddr-afe", "0")')
        page.on("dialog", lambda d: d.accept())  # window.confirm() -> always accept in this script
        try:
            page.goto(BASE)
            page.wait_for_selector('section[data-workspace="afe"]')
            ensure_expanded(page, "afe")

            print("[setup] select NMOS current mirror (has a filed .sch, no .mag yet)")
            select_topology(page, "NMOS current mirror (2-transistor)")
            page.wait_for_selector(".schematic-panel img.schematic-img", timeout=5000)

            layout_panel = page.locator(".layout-panel")
            check("layout panel is present for this block", layout_panel.count() == 1)
            layout_panel.get_by_role("button", name="Expand").click()
            page.wait_for_timeout(300)

            print("[empty state] library/cell pre-filled from the resolved schematic cell")
            lib_select = layout_panel.locator(".library-picker select")
            check("library picker pre-selected 'cal_runs' (the schematic's filed library)",
                  lib_select.input_value() == "cal_runs", lib_select.input_value())
            check("library picker is locked (a resolved library cell already exists)",
                  lib_select.is_disabled())
            cell_input = layout_panel.locator(".library-picker input[type=text]")
            check("cell name pre-filled to 'current_mirror'", cell_input.input_value() == "current_mirror",
                  cell_input.input_value())
            check("cell name input is locked", cell_input.is_disabled())

            resolution_picker = layout_panel.locator(".xschem-resolution-picker select").first
            check("display-size picker rendered before creating a session", resolution_picker.count() >= 1)
            resolution_picker.select_option("1280x800")

            print("[create] Create layout in magic")
            layout_panel.get_by_role("button", name="Create layout in magic").click()
            page.wait_for_selector(".layout-panel .xschem-live-session", timeout=20000)
            page.wait_for_selector(".layout-panel .xschem-live-canvas canvas", timeout=20000)
            source_line = layout_panel.locator(".xschem-live-session .schematic-source").inner_text()
            check("live session status line shows the requested resolution",
                  "(1280x800)" in source_line, source_line)

            # --- Lever A: view size (instant, CSS-only) ----------------------
            print("[lever A] view-size presets + fullscreen toggle")
            canvas_host = layout_panel.locator(".xschem-live-canvas")
            page.get_by_role("button", name="Small", exact=True).click()
            page.wait_for_timeout(100)
            check("clicking 'Small' applies size-small to the canvas container",
                  "size-small" in (canvas_host.get_attribute("class") or ""),
                  canvas_host.get_attribute("class"))
            page.get_by_role("button", name="Fit width", exact=True).click()
            page.wait_for_timeout(100)
            check("clicking 'Fit width' restores size-fit",
                  "size-fit" in (canvas_host.get_attribute("class") or ""))
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            layout_panel.get_by_role("button", name="Fullscreen").click()
            page.wait_for_timeout(500)
            check("clicking Fullscreen doesn't throw a page error", not errors, str(errors))
            if page.evaluate("!!document.fullscreenElement"):
                page.evaluate("document.exitFullscreen()")
                page.wait_for_timeout(300)
            check("exited fullscreen cleanly", not page.evaluate("!!document.fullscreenElement"))

            # --- Lever B: change an ALREADY-RUNNING session's resolution -----
            print("[lever B, live] change resolution on the running session (destructive restart)")
            old_session_line = layout_panel.locator(".xschem-live-session .schematic-source").inner_text()
            resize_select = layout_panel.locator(".xschem-resize-row select")
            check("live-session resolution dropdown is rendered", resize_select.count() == 1)
            resize_select.select_option("1920x1200")
            apply_btn = layout_panel.get_by_role("button", name="Apply (restarts session, unsaved work lost)")
            check("Apply button enabled once a different resolution is picked", apply_btn.is_enabled())
            apply_btn.click()
            page.wait_for_timeout(3000)  # real restart: stop + new Xvfb/magic/x11vnc spawn
            page.wait_for_selector(".layout-panel .xschem-live-canvas canvas", timeout=20000)
            new_session_line = layout_panel.locator(".xschem-live-session .schematic-source").inner_text()
            check("status line updated to the new resolution after restart",
                  "(1920x1200)" in new_session_line, new_session_line)
            check("restart produced a genuinely different session (status line text changed)",
                  new_session_line != old_session_line)

        finally:
            _restore(_snap)
            try:
                if page.evaluate("!!document.fullscreenElement"):
                    page.evaluate("document.exitFullscreen()")
                    page.wait_for_timeout(300)
            except Exception:
                pass
            stop_btn = page.locator(".layout-panel").get_by_role("button", name="Stop live session")
            if stop_btn.count():
                stop_btn.click()
                page.wait_for_timeout(500)
            browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
