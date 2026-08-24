"""Browser checks for the Firmware workspace (2026-08-23 firmware-section
spec). Script-style like the other suites in this directory - run directly,
not via pytest:

    cd backend && .venv/bin/python tests/browser_check_firmware.py

Requires both dev servers running (backend :8000, vite :5173). FREE: the
paid endpoints (POST /api/fw_chat, POST /api/fw_generate) are intercepted in
the browser via playwright routes - no agent is ever invoked.

What it asserts:
  1. The fifth stacked panel "PHY / Firmware" exists (bottom of the chain),
     expands, and renders the ser-des firmware diagram: 11 blocks incl. the
     HOST API IF / CSR IF boundary anchors, hint line, catalog optgroups in
     the Add-block picker (no models UI anywhere).
  2. Overview Firmware block: styled as software (dashed class + "SW" tag),
     sits between Controller and the SoC ext node, the control-plane caption
     is present, and clicking it focuses+expands+scrolls the firmware panel
     with the chat sidebar switching to "Firmware chat"
     (fw-chat-session-<phy> localStorage record).
  3. fw chat (intercepted): sending a message renders the stubbed reply +
     patch card; Apply adds the block to the diagram through the "fw-arch-"
     store; Undo restores it.
  4. Generate register layer (intercepted): button -> cost-note confirm ->
     stubbed PASS response renders the status/compile/test badges + file
     list + log toggle; a second stubbed FAIL response renders the failure
     plainly (FAIL badge, reason, log shown by default).
"""

import json
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"

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


FW_CHAT_STUB = {
    "session_id": "stub",
    "reply": "Run ZQ recal from a timer poll, not the IRQ handler.",
    "patch": {
        "ops": [
            {"op": "add_block", "id": "wdog", "label": "Watchdog Service", "topology": "diag_bist_orchestrator", "col": 0, "row": 1},
            {"op": "add_connection", "from": "wdog", "to": "drv"},
        ]
    },
    "patch_valid": True,
    "patch_errors": [],
    "patch_warnings": [],
    "cost_usd": 0.42,
    "duration_ms": 31000,
}

FW_GEN_PASS = {
    "status": "pass",
    "run_id": "stubrun12345",
    "reason": "",
    "files": [f"firmware/ser-des/{f}" for f in [
        "serdes_rx_if_regs.h", "serdes_rx_if_drv.h", "serdes_rx_if_drv.c",
        "reg_stub.h", "reg_stub.c", "test_serdes_rx_if.c", "Makefile"]],
    "missing_files": [],
    "compile_ok": True,
    "tests_ok": True,
    "test_output": "ok offset uniqueness\nok trim round-trip\n(exit 0)",
    "cost_usd": 1.87,
    "duration_s": 94,
}

FW_GEN_FAIL = {
    **FW_GEN_PASS,
    "status": "fail",
    "reason": "server-side verification failed: gcc -std=c99 -Wall -Werror compile failed",
    "compile_ok": False,
    "tests_ok": False,
    "test_output": "serdes_rx_if_drv.c:12: error: unused variable 'x' [-Werror]\n(exit 1)",
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 800})

        # Intercept the paid endpoints - transcript GETs pass through to the
        # real backend (free), POSTs are stubbed.
        gen_responses = iter([FW_GEN_PASS, FW_GEN_FAIL])

        def fulfill_json(route, payload):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        def route_fw_chat(route):
            if route.request.method == "POST":
                fulfill_json(route, FW_CHAT_STUB)
            else:
                route.continue_()

        def route_fw_generate(route):
            fulfill_json(route, next(gen_responses))

        page.route("**/api/fw_chat", route_fw_chat)
        page.route("**/api/fw_generate", route_fw_generate)

        page.goto(BASE)
        page.wait_for_selector('section[data-workspace="firmware"]')

        # --- 1. the fifth stacked panel ---------------------------------
        print("[panel] PHY / Firmware panel")
        sections = page.locator("section.workspace-panel").evaluate_all(
            "els => els.map(e => e.dataset.workspace)"
        )
        check(
            "firmware is the last stacked panel",
            sections[-1] == "firmware",
            f"got {sections}",
        )
        check("firmware panel minimized on load", is_minimized(page, "firmware"))
        title = panel(page, "firmware").locator("h2").inner_text()
        check("panel title", title == "PHY / Firmware", f"got {title!r}")

        # --- 2. overview Firmware block ---------------------------------
        print("[overview] Firmware block")
        fw_btn = page.locator("button.phy-overview-firmware")
        check("overview Firmware block present", fw_btn.count() == 1)
        check("SW tag present", fw_btn.locator(".phy-overview-sw-tag").inner_text() == "SW")
        border = fw_btn.evaluate("el => getComputedStyle(el).borderTopStyle")
        check("software styling: dashed border", border == "dashed", f"got {border!r}")
        row_labels = page.locator(".phy-overview-row > *").evaluate_all(
            "els => els.map(e => e.textContent.trim().slice(0, 10))"
        )
        ctrl_i = next(i for i, t in enumerate(row_labels) if t.startswith("Controller"))
        fw_i = next(i for i, t in enumerate(row_labels) if t.startswith("Firmware"))
        soc_i = next(i for i, t in enumerate(row_labels) if t.startswith("SoC core"))
        check("Firmware sits between Controller and SoC", ctrl_i < fw_i < soc_i, f"{row_labels}")
        caption = page.locator(".phy-overview-caption").inner_text()
        check(
            "control-plane caption",
            "Control plane" in caption and "Controller ⇄ SoC direct" in caption,
            f"got {caption!r}",
        )

        page.evaluate("window.scrollTo(0, 0)")
        fw_btn.click()
        page.wait_for_timeout(900)
        check("click expands the firmware panel", not is_minimized(page, "firmware"))
        cls = panel(page, "firmware").get_attribute("class") or ""
        check("click focuses the firmware panel", "focused" in cls.split())
        box = panel(page, "firmware").bounding_box()
        check("panel scrolled into view", box is not None and 0 <= box["y"] < 800, f"box={box}")
        chat_h3 = page.locator(".arch-chat-head h3").inner_text()
        check("chat sidebar switched to Firmware chat", chat_h3 == "Firmware chat", f"got {chat_h3!r}")
        sess = page.evaluate("localStorage.getItem('fw-chat-session-ser-des')")
        check("fw-chat-session-ser-des recorded", bool(sess), f"got {sess!r}")

        # --- diagram content --------------------------------------------
        print("[diagram] ser-des firmware stack")
        body = panel(page, "firmware").locator(".workspace-body")
        svg_texts = body.locator("svg text").evaluate_all("els => els.map(e => e.textContent)")
        check("HOST API IF anchor rendered", "HOST API IF" in svg_texts)
        check("CSR IF anchor rendered", "CSR IF" in svg_texts)
        for short in ["Boot Seq", "Pwr Mgr", "Train Sup", "Cal Sup", "Eye Sweep", "Diag/BIST", "IRQ", "Reg Drv", "API"]:
            check(f"block rendered: {short}", short in svg_texts)
        rect_count = body.locator("svg > g > rect").count()
        check("11 blocks drawn", rect_count == 11, f"got {rect_count}")
        check("no model checkboxes in the firmware panel", body.locator("input[type=checkbox]").count() == 0)
        hint = body.locator(".hint").first.inner_text()
        check("firmware hint line", "control plane" in hint.lower() or "register driver" in hint.lower(), f"got {hint!r}")
        # Add-block picker offers the firmware catalog optgroups.
        body.get_by_role("button", name="Add block").click()
        groups = body.locator(".arch-add-block select optgroup").evaluate_all(
            "els => els.map(e => e.label)"
        )
        check(
            "catalog optgroups in the picker",
            "Boot & power" in groups and "Calibration & training" in groups,
            f"got {groups}",
        )
        body.get_by_role("button", name="Cancel add").click()
        page.screenshot(path=f"{AUDITS}/2026-08-23-firmware-panel.png")

        # --- 3. fw chat (intercepted) -----------------------------------
        print("[chat] intercepted fw_chat turn + patch apply/undo")
        page.locator(".arch-chat-input textarea").fill("Should ZQ recal run from the IRQ handler?")
        page.locator(".arch-chat-input button").click()
        page.wait_for_selector(".chat-bubble.assistant", timeout=5000)
        reply = page.locator(".chat-bubble.assistant").last.inner_text()
        check("stubbed reply rendered", "timer poll" in reply)
        card = page.locator(".chat-patch-card").last
        check("patch card rendered", "Proposed diagram edit" in card.inner_text())
        blocks_before = body.locator("svg > g > rect").count()
        card.get_by_role("button", name="Apply").click()
        page.wait_for_timeout(300)
        blocks_after = body.locator("svg > g > rect").count()
        check("Apply added the block to the diagram", blocks_after == blocks_before + 1, f"{blocks_before}->{blocks_after}")
        fw_record = page.evaluate(
            "Object.keys(localStorage).filter(k => k.startsWith('fw-arch-')).length"
        )
        check("patch stored under fw-arch- namespace", fw_record > 0)
        page.get_by_role("button", name="Undo last patch").click()
        page.wait_for_timeout(300)
        check("Undo restored the diagram", body.locator("svg > g > rect").count() == blocks_before)

        # --- 4. generate button + stub results --------------------------
        print("[generate] intercepted fw_generate pass + fail")
        note = body.locator(".fw-generate .hint").inner_text()
        check("cost/time note next to the button", "$" in note and "minute" in note, f"got {note!r}")
        check("honesty-check note", "gcc -std=c99 -Wall -Werror" in note)
        body.get_by_role("button", name="Generate register layer").click()
        confirm = body.locator(".fw-generate-confirm")
        check("cost-bearing confirm shown", confirm.count() == 1 and "paid" in confirm.inner_text())
        confirm.get_by_role("button", name="Start run").click()
        page.wait_for_selector(".fw-result", timeout=5000)
        res = body.locator(".fw-result")
        check("PASS badge", res.locator(".fw-badge").first.inner_text() == "PASS")
        badges = res.locator(".fw-badge.sub").all_inner_texts()
        check("compile badge ok", any("compile" in b and "ok" in b for b in badges), f"{badges}")
        check("tests badge ok", any("unit tests" in b and "ok" in b for b in badges), f"{badges}")
        files = res.locator(".fw-file-list li").all_inner_texts()
        check("7 generated files listed", len(files) == 7, f"got {len(files)}")
        check("regs header listed", any("serdes_rx_if_regs.h" in f for f in files))
        check("cost + duration shown", "$1.87" in res.locator(".fw-result-meta").inner_text())
        res.get_by_role("button", name="Show compile/test log").click()
        check("log tail viewable", "trim round-trip" in res.locator(".fw-test-output").inner_text())
        page.screenshot(path=f"{AUDITS}/2026-08-23-firmware-generate-pass.png")

        # second run -> stubbed FAIL surfaces plainly
        body.get_by_role("button", name="Generate register layer").click()
        body.locator(".fw-generate-confirm").get_by_role("button", name="Start run").click()
        page.wait_for_selector(".fw-result.fail", timeout=5000)
        res = body.locator(".fw-result")
        check("FAIL badge", res.locator(".fw-badge").first.inner_text() == "FAIL")
        check("failure reason shown", "compile failed" in res.locator(".fw-result-reason").inner_text())
        check("log shown by default on failure", "unused variable" in res.locator(".fw-test-output").inner_text())
        page.screenshot(path=f"{AUDITS}/2026-08-23-firmware-generate-fail.png")

        browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
