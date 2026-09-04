"""Browser checks for focus mode ("Focus: DDR AFE only", 2026-08-27 user
scope decision). Script-style like the other suites here - run directly:

    cd backend && .venv/bin/python tests/browser_check_focus.py

Requires both dev servers running (backend :8000, vite :5173). Free: no
chat messages sent, only page loads and clicks.

What it asserts:
  1. Fresh load (clean localStorage -> focus defaults ON): PHY select shows
     "ddr" while EVERY PHY type stays selectable (focus hides workspaces,
     not PHY types - changed 2026-08-31); the AFE
     panel is EXPANDED; the interface / digital / firmware workspace
     sections are NOT rendered at all; the overview greys out + disables
     the IF / Controller / Firmware blocks and shows the focus caption;
     no RTL-generate controls anywhere.
  2. Settings toggle OFF: all three hidden workspaces reappear (with their
     usual minimized-by-default headers), overview blocks re-enable, PHY
     select options re-enable, focus caption gone. Overview navigation to
     the controller panel works again.
  3. Persistence: reload after toggling off -> still off. Toggle back ON
     -> hidden again, DDR pinned, AFE expanded, and a reload keeps it ON.
"""

import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"

HIDDEN_WS = ["interface", "digital", "firmware"]

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


def open_settings(page):
    if page.locator(".settings-panel").count() == 0:
        page.locator("button.settings-gear").click()
        page.wait_for_selector(".settings-panel")


def focus_checkbox(page):
    return page.locator(".settings-panel .focus-mode-toggle input[type=checkbox]")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 800})

        # --- 1. fresh load: focus defaults ON ---------------------------
        print("[focused] fresh load, clean localStorage")
        page.goto(BASE)
        page.wait_for_selector('section[data-workspace="overview"]')
        page.wait_for_timeout(300)

        sel = page.locator(".phy-type-select select")
        check("PHY select shows ddr", sel.input_value() == "ddr", f"got {sel.input_value()!r}")
        disabled_builtins = page.locator(".phy-type-select option[disabled]").count()
        check("no PHY type is disabled by focus", disabled_builtins == 0, f"got {disabled_builtins}")
        opts = page.locator(".phy-type-select option").count()
        check("all built-in PHY types selectable (6)", opts >= 6, f"got {opts}")

        check("AFE panel rendered + expanded", panel(page, "afe").count() == 1 and not is_minimized(page, "afe"))
        afe_h3 = page.locator(".phy-selector h3").inner_text()
        check("AFE workspace targets ddr", "ddr" in afe_h3, f"got {afe_h3!r}")
        for ws in HIDDEN_WS:
            check(f"{ws} section not rendered", panel(page, ws).count() == 0)

        dimmed = page.locator(".phy-overview .focus-dimmed").count()
        check("overview: IF/Controller/Firmware dimmed (3)", dimmed == 3, f"got {dimmed}")
        check(
            "overview: dimmed blocks disabled",
            page.locator(".phy-overview .focus-dimmed[disabled]").count() == 3,
        )
        check("overview: focus caption shown", page.locator(".focus-caption").count() == 1)
        check("no RTL-generate controls", page.locator(".rtl-generate-menu, .rtl-launch").count() == 0
              and page.get_by_text("Generate RTL", exact=False).count() == 0)
        page.screenshot(path=f"{AUDITS}/2026-08-27-focus-on-fresh-load.png", full_page=True)

        # --- 2. toggle OFF in settings ----------------------------------
        print("[unfocus] settings toggle off")
        open_settings(page)
        cb = focus_checkbox(page)
        check("settings checkbox present + checked", cb.count() == 1 and cb.is_checked())
        cb.uncheck()
        page.wait_for_timeout(300)
        for ws in HIDDEN_WS:
            check(f"{ws} section restored", panel(page, ws).count() == 1)
        check("restored panels minimized (default)", all(is_minimized(page, ws) for ws in HIDDEN_WS))
        check("overview: nothing dimmed", page.locator(".phy-overview .focus-dimmed").count() == 0)
        check("focus caption gone", page.locator(".focus-caption").count() == 0)
        check(
            "PHY select options re-enabled",
            page.locator(".phy-type-select option[disabled]").count() == 0,
        )
        check("PHY select still on ddr", sel.input_value() == "ddr")
        # overview navigation to a formerly-hidden workspace works again
        page.locator('button[title="Focus the PHY / Controller architecture workspace"]').click()
        page.wait_for_timeout(600)
        check("controller nav works when unfocused", not is_minimized(page, "digital"))
        page.screenshot(path=f"{AUDITS}/2026-08-27-focus-off-restored.png", full_page=True)

        # --- 3. persistence + toggle back ON ----------------------------
        print("[persist] reload keeps OFF; re-toggle ON persists")
        page.reload()
        page.wait_for_selector('section[data-workspace="digital"]')
        check("reload keeps focus OFF", panel(page, "digital").count() == 1)
        stored = page.evaluate('localStorage.getItem("focus-ddr-afe")')
        check("localStorage records OFF", stored == "0", f"got {stored!r}")

        # A non-DDR PHY must be selectable, and turning focus back ON must
        # NOT discard that choice - focus narrows workspaces, not PHY types.
        sel.select_option("ser-des")
        page.wait_for_timeout(300)
        check("non-DDR PHY selectable", sel.input_value() == "ser-des",
              f"got {sel.input_value()!r}")

        open_settings(page)
        focus_checkbox(page).check()
        page.wait_for_timeout(300)
        for ws in HIDDEN_WS:
            check(f"{ws} hidden again after re-toggle", panel(page, ws).count() == 0)
        check("re-toggle keeps the chosen PHY", sel.input_value() == "ser-des",
              f"got {sel.input_value()!r}")
        check("re-toggle expands AFE", not is_minimized(page, "afe"))
        page.reload()
        page.wait_for_selector('section[data-workspace="afe"]')
        page.wait_for_timeout(300)
        check("reload keeps focus ON", panel(page, "digital").count() == 0)
        check("reload: AFE expanded", not is_minimized(page, "afe"))
        # PHY choice is React state, not persisted - a reload returns to the
        # default ("ddr" while focus is on). Focus itself does persist.
        check("reload: back to default PHY", page.locator(".phy-type-select select").input_value() == "ddr")

        browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
