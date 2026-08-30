"""Browser check for the DDR channel-model panel (ChannelPanel.jsx), scoped
to that panel only. Script-style like the other browser checks here:

    cd backend && .venv/bin/python tests/browser_check_channel.py

Requires both dev servers running (backend :8000, vite :5173). Free: no chat
messages, no claude runs - it drives the panel's own file picker and waits for
a real vector fit. The fixtures are decimated reference channels, so a fit is
seconds-to-tens-of-seconds rather than the minutes a full measured file takes.
The second import of the same file exercises the cache (no refit).

What it asserts, all under the default "Focus: DDR AFE only" mode:
  1. The panel is present inside the AFE workspace (the only workspace focus
     mode renders) and opens.
  2. A .s2p imported through the file picker appears in the list, reaches a
     successful state, and shows the passivity verdict, insertion-loss table
     and cursor metrics.
  3. The raw-artifact viewer serves channel.sp (so a suspicious fit can be
     inspected).
  4. A .s4p import lands as a 4-port channel with crosstalk columns.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

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


def import_and_wait(page, fixture: Path, timeout_ms=300000):
    """Drive the panel's file picker with `fixture` and wait for the fit."""
    page.locator(".chan-file-label input[type=file]").set_input_files(str(fixture))
    page.wait_for_selector(".chan-list-item", timeout=30000)
    page.wait_for_function(
        "() => !document.querySelector('.chan-progress')",
        timeout=timeout_ms,
    )
    page.wait_for_timeout(500)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950})

        print("[focus mode] fresh load, focus defaults ON")
        page.goto(BASE)
        page.wait_for_selector('section[data-workspace="afe"]')
        page.wait_for_timeout(300)

        check("only the AFE workspace is rendered (focus mode)",
              page.locator('section[data-workspace="digital"]').count() == 0)
        panel = page.locator('section[data-workspace="afe"] .chan-section')
        check("channel panel lives inside the AFE workspace", panel.count() == 1)
        check("panel headline", "DDR channel model" in panel.inner_text())

        panel.locator("button.toggle-raw").first.click()
        page.wait_for_selector(".chan-body")
        check("panel opens", page.locator(".chan-import").count() == 1)
        check("file picker accepts touchstone only",
              page.locator(".chan-file-label input[type=file]").get_attribute("accept")
              == ".s2p,.s4p")

        print("[import] 2-port .s2p through the file picker")
        import_and_wait(page, FIXTURES / "chan_2port.s2p")
        check("channel listed", page.locator(".chan-list-item").count() >= 1)
        check("no failure box", page.locator(".chan-body .error-box").count() == 0)
        verdict = page.locator(".chan-verdict")
        check("passivity verdict shown", verdict.count() == 1)
        check("verdict is Passive", "Passive" in verdict.inner_text()
              and "NOT passive" not in verdict.inner_text(), verdict.inner_text())
        check("max singular value shown", "max singular value" in verdict.inner_text())
        metrics = page.locator(".chan-metrics").inner_text()
        check("insertion loss table", page.locator(".chan-table").count() == 1)
        check("2-port subckt named ddr_chan", "ddr_chan" in metrics, metrics[:200])
        # innerText is the RENDERED text, and the metric labels are
        # text-transform: uppercase - compare case-insensitively.
        check("cursor metrics shown",
              "h0 (main cursor)" in metrics.lower() and "sum |isi|" in metrics.lower(),
              metrics[:300])
        check("artifact paths shown", page.locator(".chan-contract").count() == 1)
        page.screenshot(path=f"{AUDITS}/2026-08-29-channel-2port.png", full_page=True)

        print("[artifacts] raw viewer")
        page.get_by_role("button", name="SPICE subckt").click()
        page.wait_for_selector(".chan-raw")
        page.wait_for_function(
            "() => { const e = document.querySelector('.chan-raw');"
            " return e && e.textContent.trim() !== 'loading...'; }",
            timeout=30000,
        )
        raw = page.locator(".chan-raw").inner_text()
        check("channel.sp served to the UI", ".SUBCKT ddr_chan" in raw, raw[:120])
        page.get_by_role("button", name="Cursors").click()
        page.wait_for_timeout(800)
        check("cursors.txt served", len(page.locator(".chan-raw").inner_text().split()) > 10)

        print("[import] 4-port .s4p")
        import_and_wait(page, FIXTURES / "chan_4port.s4p")
        metrics4 = page.locator(".chan-metrics").inner_text()
        check("4-port subckt named ddr_chan4", "ddr_chan4" in metrics4, metrics4[:200])
        check("crosstalk columns present",
              "NEXT" in page.locator(".chan-table").inner_text()
              and "FEXT" in page.locator(".chan-table").inner_text())
        check("two channels listed", page.locator(".chan-list-item").count() >= 2)
        page.screenshot(path=f"{AUDITS}/2026-08-29-channel-4port.png", full_page=True)

        print("[cache] re-importing the same file")
        page.locator(".chan-file-label input[type=file]").set_input_files(
            str(FIXTURES / "chan_2port.s2p")
        )
        page.wait_for_timeout(1500)
        check("cache hit: no refit progress box", page.locator(".chan-progress").count() == 0)
        check("cache hit: still just two channels",
              page.locator(".chan-list-item").count() == 2,
              str(page.locator(".chan-list-item").count()))
        check("cache hit: metrics still shown", page.locator(".chan-verdict").count() == 1)

        print("[focus] spec form still works alongside the panel")
        check("spec form still rendered",
              page.locator('section[data-workspace="afe"] .phy-selector').count() == 1)

        browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
