"""Browser checks for the top-level "PHY architecture" overview panel
(2026-08-23 UX fix + selector move). Script-style like the other suites in
this directory - run directly, not via pytest:

    cd backend && .venv/bin/python tests/browser_check_overview.py

Requires both dev servers running (backend :8000, vite :5173). Free: no
chat messages are sent, only page loads and clicks.

What it asserts:
  0. Collapsed-by-default load (user request, 2026-08-23): a fresh page
     shows exactly ONE expanded panel - the overview - with all four
     workspace panels (AFE / interface / Controller / Firmware) minimized.
  1. Overview-block navigation: for each of Controller / AFE / IF /
     Firmware, minimize the target workspace panel, scroll to the top,
     click the overview block -> the target panel is un-minimized, focused
     (accent + "chat follows this panel"), scrolled into view (bounding-box
     y within the viewport), and the chat sidebar switched to that
     workspace's chat.
  2. The PHY Type / Architecture pull-down lives INSIDE the overview
     panel's header (moved out of the spec form), works (switching to
     "optical" re-targets the workspaces), and stays reachable while the
     overview panel is minimized.
"""

import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"

# overview button title -> (workspace id, chat sidebar title)
NAV_CASES = [
    ("Focus the PHY / Controller architecture workspace", "digital", "Controller architecture chat"),
    ("Focus the PHY / AFE architecture workspace", "afe", "AFE architecture chat"),
    ("Focus the AFE ↔ Controller interface workspace", "interface", "Interface chat"),
    ("Focus the PHY / Firmware workspace", "firmware", "Firmware chat"),
]

WORKSPACE_IDS = ["afe", "interface", "digital", "firmware"]

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


def set_minimized(page, ws, want):
    if is_minimized(page, ws) != want:
        panel(page, ws).locator("button.workspace-min-btn").click()
        page.wait_for_timeout(150)


def wait_scroll_settled(page, timeout_ms=4000):
    """Wait for the smooth scroll to finish (scrollY stable across frames)."""
    deadline = time.monotonic() + timeout_ms / 1000
    last = page.evaluate("window.scrollY")
    while time.monotonic() < deadline:
        page.wait_for_timeout(200)
        cur = page.evaluate("window.scrollY")
        if cur == last:
            return
        last = cur


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 800})
        page.goto(BASE)
        page.wait_for_selector('section[data-workspace="overview"]')
        page.wait_for_selector('section[data-workspace="digital"]')
        vh = 800

        # --- 0. collapsed-by-default fresh load --------------------------
        print("[default] fresh load: only the overview expanded")
        check("overview: expanded on load", not is_minimized(page, "overview"))
        for ws in WORKSPACE_IDS:
            check(f"{ws}: minimized on load", is_minimized(page, ws))
        expanded = page.locator("section.workspace-panel:not(.minimized)").count()
        check("exactly one expanded panel on load", expanded == 1, f"got {expanded}")

        # --- 1. overview-block click: un-minimize + scroll into view -----
        for title, ws, chat_title in NAV_CASES:
            print(f"[nav] overview -> {ws}")
            set_minimized(page, ws, True)
            check(f"{ws}: pre-minimized", is_minimized(page, ws))
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
            page.locator(f'button[title="{title}"]').click()
            wait_scroll_settled(page)

            check(f"{ws}: un-minimized after click", not is_minimized(page, ws))
            cls = panel(page, ws).get_attribute("class") or ""
            check(f"{ws}: focused accent", "focused" in cls.split())
            box = panel(page, ws).bounding_box()
            check(
                f"{ws}: scrolled into view (y={box['y']:.0f})",
                box is not None and 0 <= box["y"] < vh,
                f"box={box}",
            )
            hint = panel(page, ws).locator(".workspace-focus-hint").inner_text()
            check(f"{ws}: 'chat follows this panel' hint", "chat follows" in hint)
            chat_h3 = page.locator(".arch-chat-head h3").inner_text()
            check(f"{ws}: chat sidebar switched", chat_h3 == chat_title, f"got {chat_h3!r}")
            if ws == "digital":
                page.screenshot(path=f"{AUDITS}/2026-08-23-overview-nav-controller.png")

        # re-clicking an already-visible block is a harmless no-op (still focused)
        page.locator(f'button[title="{NAV_CASES[2][0]}"]').click()
        wait_scroll_settled(page)
        check("interface: repeat click keeps focus", "focused" in (panel(page, "interface").get_attribute("class") or ""))

        # --- 2. PHY selector inside the overview panel -------------------
        print("[selector] placement + behavior")
        sel = page.locator('section[data-workspace="overview"] .workspace-head .phy-type-select select')
        check("selector inside overview panel header", sel.count() == 1)
        check("selector no longer in the spec form", page.locator("form.spec-form .phy-type-select").count() == 0)
        check(
            "exactly one PHY Type / Architecture control on the page",
            page.locator(".phy-type-select").count() == 1,
        )

        sel.select_option("optical")
        page.wait_for_timeout(400)
        afe_h3 = page.locator(".phy-selector h3").inner_text()
        check("AFE workspace switched to optical", "optical" in afe_h3, f"got {afe_h3!r}")
        if_name = page.locator(".if-name-label input").input_value()
        check("interface workspace switched to optical", if_name == "optical-rx-if", f"got {if_name!r}")
        dig_hint = panel(page, "digital").locator(".workspace-body .hint").first.inner_text()
        check("controller workspace switched to optical", "TIA/LA" in dig_hint, f"got {dig_hint!r}")

        # reachable while the overview panel is minimized
        set_minimized(page, "overview", True)
        check("selector visible while overview minimized", sel.is_visible())
        sel.select_option("ser-des")
        page.wait_for_timeout(400)
        afe_h3 = page.locator(".phy-selector h3").inner_text()
        check("PHY switch works while overview minimized", "ser-des" in afe_h3, f"got {afe_h3!r}")
        page.screenshot(path=f"{AUDITS}/2026-08-23-overview-selector-minimized.png")
        set_minimized(page, "overview", False)
        page.screenshot(path=f"{AUDITS}/2026-08-23-overview-selector-in-panel.png")

        browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
