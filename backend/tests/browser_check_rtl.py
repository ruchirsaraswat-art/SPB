"""Browser checks for spec-document intake + RTL generation (2026-08-23
rtl-gen spec). Script-style like the other suites in this directory - run
directly, not via pytest:

    cd backend && .venv/bin/python tests/browser_check_rtl.py

Requires both dev servers running (backend :8000, vite :5173). FREE: every
network call this flow makes (spec_docs GET/POST, libraries, rtl_generate,
the run poll) is intercepted with playwright routes - no agent is ever
invoked and no server state is touched.

What it asserts:
  1. Spec-doc intake banner in the Controller panel: shows when the per-PHY
     answer is null, has the three choice buttons + dismiss x; clicking
     "No spec - first principles" POSTs the answer and suppresses the banner.
  2. "Spec docs (N)" toolbar button reflects the doc count and opens the
     manager listing the stubbed doc.
  3. Block RTL menu: selecting the Elastic FIFO diagram block opens the
     catalog-fields mini-form; values persist in localStorage
     dig-fields-<phy>; required-field gaps disable Generate with an inline
     list; filling them + picking a library enables it; the confirm card
     carries the $2-5 / 30 min wording; Start run fires POST /api/rtl_generate
     with scope=block + the persisted block_fields.
  4. "Generate controller RTL" toolbar button opens the confirm card with the
     verbatim most-expensive-run cost line, the included-blocks list, the
     missing-fields blocking list, and stays disabled until a library is
     chosen.
  5. Stubbed run results render: RTL section with per-block verdict badges,
     checks, the server-downgrade reason, spec-citation chips, findings, the
     filed-cell links and the amber `partial` badge.
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


SPEC_DOC = {
    "id": "jesd209-4b",
    "title": "JESD209-4B LPDDR4",
    "standard_family": "JEDEC",
    "version": "B, Feb 2017",
    "source": {"type": "reference", "citation": "JESD209-4B section 7"},
    "user_notes": "x16 subset",
    "relevant_sections": [
        {"label": "Mode registers", "locator": "section 7.2, pages 45-58",
         "applies_to": ["controller"]}],
    "applies_to_workspaces": ["controller", "interface"],
    "phy_type": "ser-des",
    "added_at": "2026-08-23T14:02:00Z",
}

RTL_RUN = {
    "run_id": "stubrtl000001",
    "label": "RTL - rx-fifo",
    "state": "done",
    "status": "partial",
    "reason": "blocks failed server re-verification: rx_align - verified blocks are still filed",
    "deliverable": "rtl",
    "light_run": False,
    "warnings": [],
    "created_at": "2026-08-23T15:00:00Z",
    "finished_at": "2026-08-23T15:20:00Z",
    "cost_usd": 3.21,
    "duration_ms": 1200000,
    "raw_text": "Generated the FIFO and aligner.",
    "artifacts": {"rtl_summary": "rtl_summary.json"},
    "spec": {
        "scope": "controller",
        "phy_type": "ser-des",
        "deliverable": "rtl",
        "interface": {"signals": [{"name": f"s{i}"} for i in range(19)]},
    },
    "summary": {
        "scope": "controller",
        "phy_type": "ser-des",
        "blocks": [
            {"block": "rx_fifo", "topology": "elastic_fifo",
             "files": {"rtl": "rx_fifo.v", "tb": "rx_fifo_tb.sv", "sim_log": "rx_fifo_sim.log"},
             "status": "verified", "status_reason": "",
             "checks": [{"name": "fill_to_full", "result": "pass"},
                        {"name": "ppm_stress", "result": "pass"}],
             "spec_citations": ["JESD209-4B 7.2"]},
            {"block": "rx_align", "topology": "word_aligner",
             "files": {"rtl": "rx_align.v", "tb": "rx_align_tb.sv", "sim_log": "rx_align_sim.log"},
             "status": "failed",
             "status_reason": "claimed verified; server re-run failed: RTL_TB_FAIL realign",
             "checks": [{"name": "lock_all_offsets", "result": "pass"},
                        {"name": "realign", "result": "fail"}],
             "spec_citations": []},
        ],
        "top": {"module": "ser_des_controller_top",
                "files": {"rtl": "ser_des_controller_top.v",
                          "tb": "ser_des_controller_top_tb.sv",
                          "sim_log": "ser_des_controller_top_sim.log"},
                "status": "verified", "status_reason": "",
                "checks": [{"name": "port_conformance", "result": "pass"}]},
        "findings": ["edge csr->train has no named signal contract in the interface"],
        "filed_cells": ["serdes_ctrl/rx_fifo", "serdes_ctrl/ser_des_controller_top"],
    },
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})

        state = {"answer": None, "docs": [], "rtl_requests": []}

        def fulfill_json(route, payload):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        def route_spec_docs(route):
            req = route.request
            if req.method == "GET":
                fulfill_json(route, {"docs": state["docs"],
                                     "status": {"ser-des": {"answer": state["answer"],
                                                            "answered_at": None}}})
            elif req.method == "POST" and req.url.endswith("/answer"):
                state["answer"] = json.loads(req.post_data)["answer"]
                fulfill_json(route, {"answer": state["answer"], "answered_at": "now"})
            else:
                route.continue_()

        def route_libraries(route):
            fulfill_json(route, {"libraries": [{"name": "serdes_ctrl", "cells": []}]})

        def route_rtl_generate(route):
            state["rtl_requests"].append(json.loads(route.request.post_data))
            fulfill_json(route, {"run_id": RTL_RUN["run_id"]})

        def route_run_detail(route):
            fulfill_json(route, RTL_RUN)

        page.route("**/api/spec_docs*", route_spec_docs)
        page.route("**/api/spec_docs/**", route_spec_docs)
        page.route("**/api/libraries", route_libraries)
        page.route("**/api/rtl_generate", route_rtl_generate)
        page.route(f"**/api/runs/{RTL_RUN['run_id']}", route_run_detail)

        page.goto(BASE)
        page.wait_for_selector('section[data-workspace="digital"]')

        # Expand the Controller panel (fresh load: only the overview expanded).
        dig = panel(page, "digital")
        check("digital panel minimized on load", "minimized" in (dig.get_attribute("class") or ""))
        dig.locator("button.workspace-min-btn").click()
        page.wait_for_selector(".digital-arch-panel")

        # --- 1. intake banner --------------------------------------------
        print("[banner] spec-doc intake")
        banner = page.locator(".spec-docs-banner")
        check("banner shown when answer is null", banner.count() == 1)
        text = banner.inner_text()
        check("banner wording", "specification document" in text and "JEDEC, PCIe, UCIe, CXL" in text, text[:120])
        for label in ["Attach file", "Paste reference", "No spec - first principles"]:
            check(f"banner button: {label}", banner.get_by_role("button", name=label).count() == 1)
        check("dismiss x present", banner.locator(".spec-docs-dismiss").count() == 1)

        banner.get_by_role("button", name="No spec - first principles").click()
        page.wait_for_timeout(400)
        check("no-spec answer POSTed", state["answer"] == "no_spec")
        check("banner suppressed after explicit choice", page.locator(".spec-docs-banner").count() == 0)

        # --- 2. Spec docs (N) manager ------------------------------------
        print("[manager] Spec docs (N)")
        btn = page.locator(".spec-docs-btn")
        check("Spec docs (0) count", btn.inner_text().strip() == "Spec docs (0)", btn.inner_text())
        state["docs"] = [SPEC_DOC]
        # trigger a refresh via open (manager fetch happens on change) - reload cheapest
        page.reload()
        page.wait_for_selector('section[data-workspace="digital"]')
        panel(page, "digital").locator("button.workspace-min-btn").click()
        page.wait_for_selector(".digital-arch-panel")
        check("banner stays suppressed on reload (no_spec recorded)",
              page.locator(".spec-docs-banner").count() == 0)
        btn = page.locator(".spec-docs-btn")
        check("Spec docs (1) count", btn.inner_text().strip() == "Spec docs (1)", btn.inner_text())
        btn.click()
        mgr = page.locator(".spec-docs-manager")
        check("manager lists the doc", "JESD209-4B LPDDR4" in mgr.inner_text())
        check("manager shows sections", "section 7.2, pages 45-58" in mgr.inner_text())
        mgr.get_by_role("button", name="Close").click()

        # --- 3. Block RTL menu -------------------------------------------
        print("[block-menu] Elastic FIFO selection + fields persistence")
        body = panel(page, "digital").locator(".workspace-body")
        page.evaluate("localStorage.removeItem('dig-fields-ser-des')")
        # The block label has pointer-events:none (the rect takes the click) -
        # click at the label's coordinates so the rect underneath receives it.
        box = body.locator("svg text", has_text="Elastic FIFO").first.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_selector(".rtl-block-menu")
        menu = page.locator(".rtl-block-menu")
        check("menu names the block", "Block RTL" in menu.inner_text() and "elastic_fifo" in menu.inner_text())
        check("spec-doc chip shown", menu.locator(".spec-chip").inner_text() == "JESD209-4B LPDDR4")
        gen_btn = menu.locator(".rtl-generate-btn")
        check("generate disabled with missing fields", gen_btn.is_disabled())
        missing = menu.locator(".rtl-missing-fields").inner_text()
        check("missing required fields listed inline",
              "depth_words" in missing and "width_bits" in missing and "ppm_tolerance" in missing,
              missing)
        menu.locator('input[data-field="depth_words"]').fill("16")
        menu.locator('input[data-field="width_bits"]').fill("16")
        menu.locator('input[data-field="ppm_tolerance"]').fill("300")
        stored = page.evaluate("JSON.parse(localStorage.getItem('dig-fields-ser-des'))")
        check("fields persisted in dig-fields-ser-des",
              stored and stored.get("rx-fifo", {}).get("depth_words") == "16", str(stored))
        check("missing-fields note gone", menu.locator(".rtl-missing-fields").count() == 0)
        check("still disabled without a library", gen_btn.is_disabled())
        menu.locator("select").select_option("serdes_ctrl")
        check("enabled once fields + library set", gen_btn.is_enabled())
        gen_btn.click()
        confirm = menu.locator(".rtl-confirm-card")
        check("block cost confirm", "$2-5" in confirm.inner_text() and "30 min" in confirm.inner_text())
        page.screenshot(path=f"{AUDITS}/2026-08-23-rtl-block-menu.png")
        confirm.get_by_role("button", name="Start run").click()
        page.wait_for_timeout(500)
        check("rtl_generate POSTed", len(state["rtl_requests"]) == 1)
        req = state["rtl_requests"][0]
        check("scope=block + block_id", req.get("scope") == "block" and req.get("block_id") == "rx-fifo")
        check("block_fields carried from localStorage",
              req.get("block_fields", {}).get("rx-fifo", {}).get("depth_words") == "16")
        check("library + spec docs in request",
              req.get("library") == "serdes_ctrl" and req.get("spec_doc_ids") == ["jesd209-4b"])
        check("diagram + interface in request",
              len(req.get("digital_architecture", {}).get("blocks", [])) > 0
              and "signals" in req.get("interface", {}))

        # --- 5. stubbed results render -----------------------------------
        print("[results] stubbed rtl run rendering")
        page.wait_for_selector(".rtl-section", timeout=8000)
        rv = page.locator(".results-view")
        check("partial header", "Run partially succeeded" in rv.inner_text())
        check("RTL - controller badge",
              "rtl - controller" in rv.locator(".deliverable-badge").first.inner_text().lower())
        check("amber partial badge", rv.locator(".partial-badge").count() == 1)
        rows = rv.locator(".rtl-result-row")
        check("top row first with module name", "top: ser_des_controller_top" in rows.first.inner_text())
        check("port-count confirmation note", "all 19 interface signals" in rows.first.inner_text())
        badges = [b.lower() for b in rv.locator(".rtl-result-row .model-status-badge").all_inner_texts()]
        check("verdict badges per row", badges == ["verified", "verified", "failed"], str(badges))
        check("downgrade reason prominent",
              "claimed verified; server re-run failed" in rv.locator(".rtl-downgrade").inner_text())
        checks_txt = rv.locator(".rtl-checks").all_inner_texts()
        check("checks rendered", any("ppm_stress: pass" in t for t in checks_txt)
              and any("realign: fail" in t for t in checks_txt), str(checks_txt))
        check("spec-citation chip", rv.locator(".rtl-citations .spec-chip").inner_text() == "JESD209-4B 7.2")
        check("findings verbatim", "edge csr->train has no named signal contract" in rv.locator(".rtl-findings").inner_text())
        filed = rv.locator(".rtl-filed").inner_text()
        check("filed cells listed", "serdes_ctrl/rx_fifo" in filed and "serdes_ctrl/ser_des_controller_top" in filed)
        page.screenshot(path=f"{AUDITS}/2026-08-23-rtl-results.png", full_page=False)

        # --- 4. controller confirm card ----------------------------------
        print("[controller] Generate controller RTL confirm card")
        page.evaluate("localStorage.setItem('dig-fields-ser-des', JSON.stringify({'rx-fifo': {depth_words: 16, width_bits: 16, ppm_tolerance: 300}}))")
        page.locator(".rtl-controller-btn").click()
        card = page.locator(".rtl-controller-card")
        check("card opens", card.count() == 1)
        txt = card.inner_text()
        # Estimate recalibrated from the first real block run ($4.21 / 8.2 min
        # for elastic_fifo, 2026-08-24) - see the spec's calibration appendix.
        check("verbatim cost line (recalibrated)",
              "This is the most expensive run type in this tool: estimated $25-55 and 60-120 minutes"
              in txt and "It generates and verifies every block plus the stitched top." in txt)
        check("designable blocks listed", "designable blocks included" in txt and "Elastic FIFO" in txt)
        check("iface anchors skipped note", "Skipped automatically" in txt and "AFE RX IF" in txt)
        check("missing-fields blocking list",
              "Generation blocked - blocks with missing required fields" in txt
              and "rx-align" in txt, txt[:400])
        check("spec-doc chips in card", card.locator(".spec-chip").count() >= 1)
        card.locator("select").select_option("serdes_ctrl")
        gen = card.locator(".rtl-controller-actions button").first
        check("generate stays disabled while fields incomplete", gen.is_disabled())
        page.screenshot(path=f"{AUDITS}/2026-08-23-rtl-controller-card.png")

        browser.close()

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
