"""Browser check for the live xschem-session resize UI (2026-09 resize
feature - see backend/xschem_session.py's restart_session() and
frontend/src/SchematicPanel.jsx). Script-style like the other suites here:

    cd backend && .venv/bin/python tests/browser_check_xschem_resize.py

Requires the SINGLE-ORIGIN server at :8000 (./run.sh, or a bare `uvicorn
main:app --port 8000` against a built frontend/dist) - NOT the :5173 Vite
dev server. Verified empirically (2026-09): the noVNC client is loaded via a
runtime `import()` of a plain static asset under frontend/public/novnc/ (see
SchematicPanel.jsx's big comment on that), and Vite's OWN dev-server
transform pipeline explicitly refuses to serve anything under /public/
through `import()` ("this file is in /public ... should not be imported
from source code") - a real Vite limitation, not a bug in this feature.
Every other browser_check_*.py in this dir targets :5173 because they never
touch the live xschem session; this one specifically needs :8000. Unlike
those, this one genuinely starts/stops real interactive xschem sessions
(Xvfb + xschem + x11vnc on PATH - this box has them), so it cleans up after
itself in a `finally` block.

Exercises BOTH resize levers end to end through the real UI:
  A. View size (CSS-only, instant): the Small/Medium/Large/Fit-width preset
     buttons change the canvas container's class, and Fullscreen toggles
     without throwing.
  B. Display resolution (destructive restart): the "Display resolution"
     picker + Apply button, gated behind a real window.confirm() dialog -
     accepting it restarts the session at a new size (new session_id,
     updated display/resolution shown in the status line).
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


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.add_init_script('localStorage.setItem("focus-ddr-afe", "0")')
        page.on("dialog", lambda d: d.accept())  # window.confirm() -> always accept in this script
        live_session_id = None
        try:
            page.goto(BASE)
            page.wait_for_selector('section[data-workspace="afe"]')
            ensure_expanded(page, "afe")

            print("[setup] select NMOS current mirror (has a filed .sch)")
            select_topology(page, "NMOS current mirror (2-transistor)")
            page.wait_for_selector(".schematic-panel img.schematic-img", timeout=5000)

            # --- new-session resolution picker -------------------------------
            print("[lever B, pre-start] display-size picker next to 'Edit in xschem'")
            picker = page.locator(".xschem-live-actions .xschem-resolution-picker select")
            check("resolution picker rendered before starting a session", picker.count() == 1)
            options = picker.locator("option").all_inner_texts() if picker.count() else []
            check("resolution picker offers the expected presets",
                  set(options) == {"1280x800", "1600x1000", "1920x1200", "2560x1600"}, str(options))
            picker.select_option("1280x800")

            print("[start] Edit in xschem (live, in browser)")
            page.get_by_role("button", name="Edit in xschem (live, in browser)").click()
            page.wait_for_selector(".xschem-live-session", timeout=15000)
            page.wait_for_selector(".xschem-live-canvas canvas", timeout=15000)
            source_line = page.locator(".xschem-live-session .schematic-source").inner_text()
            check("live session status line shows the requested resolution",
                  "(1280x800)" in source_line, source_line)

            # --- Lever A: view size (instant, CSS-only) ----------------------
            print("[lever A] view-size presets + fullscreen toggle")
            canvas_host = page.locator(".xschem-live-canvas")
            check("canvas defaults to 'fit' view size", "size-fit" in (canvas_host.get_attribute("class") or ""))
            page.get_by_role("button", name="Small", exact=True).click()
            page.wait_for_timeout(100)
            check("clicking 'Small' applies size-small to the canvas container",
                  "size-small" in (canvas_host.get_attribute("class") or ""),
                  canvas_host.get_attribute("class"))
            page.get_by_role("button", name="Large", exact=True).click()
            page.wait_for_timeout(100)
            check("clicking 'Large' applies size-large",
                  "size-large" in (canvas_host.get_attribute("class") or ""),
                  canvas_host.get_attribute("class"))
            page.get_by_role("button", name="Fit width", exact=True).click()
            page.wait_for_timeout(100)
            check("clicking 'Fit width' restores size-fit",
                  "size-fit" in (canvas_host.get_attribute("class") or ""))
            # Fullscreen: headless Chromium may refuse an unattended
            # requestFullscreen() - the button must not crash the page
            # either way; treat "no JS error" as the pass condition (a real
            # attended browser session behaves per the earlier api.js/CSS
            # verification already covered by manual + Xlib live-proof).
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.get_by_role("button", name="Fullscreen").click()
            page.wait_for_timeout(500)
            check("clicking Fullscreen doesn't throw a page error", not errors, str(errors))
            # Headless Chromium genuinely enters real Fullscreen API state here
            # (not just a CSS class - document.fullscreenElement really is the
            # canvas host). Per the Fullscreen spec, that makes the REST of
            # the page - including the "Exit fullscreen" button itself, which
            # lives outside the fullscreen element in the DOM - unclickable
            # by a synthesized mouse event; a real user exits via Esc (or the
            # button, while the browser chrome overlay is showing) - script
            # this the same way Esc would work: call the DOM API directly.
            if page.evaluate("!!document.fullscreenElement"):
                page.evaluate("document.exitFullscreen()")
                page.wait_for_timeout(300)
            check("exited fullscreen cleanly", not page.evaluate("!!document.fullscreenElement"))

            # --- Lever B: change an ALREADY-RUNNING session's resolution -----
            print("[lever B, live] change resolution on the running session (destructive restart)")
            old_session_line = page.locator(".xschem-live-session .schematic-source").inner_text()
            resize_select = page.locator(".xschem-resize-row select")
            check("live-session resolution dropdown is rendered", resize_select.count() == 1)
            resize_select.select_option("1920x1200")
            apply_btn = page.get_by_role("button", name="Apply (restarts session, unsaved work lost)")
            check("Apply button enabled once a different resolution is picked", apply_btn.is_enabled())
            apply_btn.click()  # triggers window.confirm() -> auto-accepted by the dialog handler above
            page.wait_for_timeout(3000)  # real restart: stop + new Xvfb/xschem/x11vnc spawn
            page.wait_for_selector(".xschem-live-canvas canvas", timeout=15000)
            new_session_line = page.locator(".xschem-live-session .schematic-source").inner_text()
            check("status line updated to the new resolution after restart",
                  "(1920x1200)" in new_session_line, new_session_line)
            check("restart produced a genuinely different session (status line text changed)",
                  new_session_line != old_session_line)

        finally:
            # Best-effort: stop whatever live session is still running so this
            # check never leaks a real Xvfb/xschem/x11vnc triple. Exit
            # fullscreen first if a failed assertion left it engaged - the
            # rest of the page is unclickable while it's active.
            try:
                if page.evaluate("!!document.fullscreenElement"):
                    page.evaluate("document.exitFullscreen()")
                    page.wait_for_timeout(300)
            except Exception:
                pass
            stop_btn = page.get_by_role("button", name="Stop live session")
            if stop_btn.count():
                stop_btn.click()
                page.wait_for_timeout(500)
            browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
