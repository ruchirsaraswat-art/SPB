"""Browser checks for the settings panel's libraries_dir split (cycle 6, user
request: "add an option for user to specify the work area and the location
of the libraries") and the model/deliverable flow it sits next to (bmod
generation fix scoping). Script-style like the other suites here - run
directly:

    cd backend && .venv/bin/python tests/browser_check_settings_libdir.py

Requires both dev servers running (backend :8000, vite :5173). Restores the
real settings (clears the libraries_dir override) before exiting, whatever
happens - the live tool's working_dir/libraries_dir are never left pointed at
a scratch directory.

What it asserts:
  1. Settings panel shows two labeled fields: "Work area" (renamed from
     "Working directory") and "Libraries location" (optional, placeholder =
     the derived default). Setting a libraries_dir override updates the
     "Design libraries" derived line and shows a hint that it is an override
     of the work-area default; clearing it reverts.
  2. For a plain (non-architecture) circuit spec with a topology picked
     directly from the Topology dropdown, the Generate menu offers the model
     deliverables and the model checkboxes render, per-topology-applicable
     ones enabled (not a silently-all-disabled matrix).
  3. The submit button is disabled with an explanatory hint (not silently
     dead) until a design library is chosen, and enables once one is picked.
"""

import shutil
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:5173"
API = "http://127.0.0.1:8000"
AUDITS = "/home/rsarasw1/analog-spec-tool/audits"
SCRATCH_LIBS = "/tmp/claude-1001/-home-rsarasw1/e37c41e7-5858-4d1f-9e11-70af139aa4be/scratchpad/browser_check_libs_override"

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


def open_settings(page):
    if page.locator(".settings-panel").count() == 0:
        page.locator("button.settings-gear").click()
        page.wait_for_selector(".settings-panel")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            # --- 1. settings panel: two fields, override round-trip --------
            print("[settings] work area + libraries location fields")
            page.goto(BASE)
            page.wait_for_selector('section[data-workspace="overview"]')
            open_settings(page)

            labels = page.locator(".settings-panel label").all_inner_texts()
            check(
                "Work area label present",
                any("Work area" in t for t in labels),
                labels,
            )
            check(
                "Libraries location label present",
                any("Libraries location" in t for t in labels),
                labels,
            )

            lib_input = page.get_by_test_id("libraries-location-input")
            check("libraries_dir input starts empty", lib_input.input_value() == "")
            placeholder = lib_input.get_attribute("placeholder") or ""
            check(
                "placeholder shows the derived default",
                placeholder.endswith("/libraries"),
                placeholder,
            )
            check(
                "unset hint shown",
                page.get_by_text("Unset - libraries are filed under the work area", exact=False).count() == 1,
            )

            lib_input.fill(SCRATCH_LIBS)
            page.locator(".settings-panel button", has_text="Save").click()
            page.wait_for_timeout(400)
            check(
                "saved tick shown after override save",
                page.locator(".settings-saved").count() == 1,
            )
            design_lib_line = page.locator(".settings-derived li").first.inner_text()
            check(
                "Design libraries derived line reflects the override",
                SCRATCH_LIBS in design_lib_line,
                design_lib_line,
            )
            check(
                "override hint shown",
                page.get_by_text("Overriding the work area's default", exact=False).count() == 1,
            )
            resp = page.request.get(f"{API}/api/settings").json()
            check(
                "backend persisted the override",
                resp["libraries_dir"] == SCRATCH_LIBS and resp["libraries_root"] == SCRATCH_LIBS,
                resp,
            )
            page.screenshot(path=f"{AUDITS}/2026-08-28-settings-libdir-override.png", full_page=True)

            # clear it back
            lib_input.fill("")
            page.locator(".settings-panel button", has_text="Save").click()
            page.wait_for_timeout(400)
            resp = page.request.get(f"{API}/api/settings").json()
            check(
                "clearing reverts backend to derived default",
                resp["libraries_dir"] is None and resp["libraries_root"] == resp["default_libraries_dir"],
                resp,
            )
            check(
                "cleared override still listed as a previous libraries location",
                any(p["libraries_dir"] == SCRATCH_LIBS for p in resp["previous_libraries_dirs"]),
                resp["previous_libraries_dirs"],
            )
            page.locator("button.settings-close").click()

            # --- 2. model/deliverable flow for a plain circuit spec --------
            print("[deliverable] model checkboxes + Generate menu for a plain topology")
            page.locator(".spec-form select").first.select_option("ctle")
            page.wait_for_timeout(300)

            checkboxes = page.locator(".model-choice")
            check("model checkbox group renders (3 types)", checkboxes.count() == 3, checkboxes.count())
            va = page.locator(".model-choice", has_text="Verilog-A").locator("input")
            dv = page.locator(".model-choice", has_text="Digital Verilog").locator("input")
            rnm = page.locator(".model-choice", has_text="RNM").locator("input")
            check("Verilog-A enabled for ctle (continuous analog)", va.is_enabled())
            check("RNM always enabled", rnm.is_enabled())
            check("Digital Verilog correctly disabled for ctle (analog block)", dv.is_disabled())
            reason = page.locator(".model-choice", has_text="Digital Verilog").locator(".model-disabled-reason").inner_text()
            check("disabled reason is a real explanation, not empty", len(reason) > 20, reason)

            gen_select = page.locator("select.generate-mode, .generate-mode select")
            options = gen_select.locator("option").all_inner_texts()
            check(
                "Generate dropdown lists the model-only options",
                any("Verilog-A model only" in o for o in options)
                and any("RNM model only" in o for o in options),
                options,
            )
            rnm_option = gen_select.locator("option", has_text="RNM model only")
            check("RNM model-only option not disabled for ctle", rnm_option.get_attribute("disabled") is None)

            # --- 3. submit button gated + explained until a library is set -
            print("[gate] submit button disabled + explained without a library")
            submit_btn = page.locator(".spec-form button[type=submit]")
            check("submit disabled with no library chosen", submit_btn.is_disabled())
            check(
                "explanatory hint shown next to the disabled submit button",
                page.get_by_text("Choose (or create) a design library above", exact=False).count() == 1,
            )

            gen_select.select_option("rnm")
            page.wait_for_timeout(150)
            check("still disabled for model-only deliverable too (no library yet)", submit_btn.is_disabled())

            new_lib_name = "browser_check_tmp_lib"
            page.locator(".library-picker select").select_option("__new__")
            page.locator(".new-library-row input").fill(new_lib_name)
            page.locator(".new-library-row button", has_text="Create library").click()
            page.wait_for_timeout(600)
            check("submit enabled once a library is chosen", submit_btn.is_enabled())
            check(
                "hint gone once library is chosen",
                page.get_by_text("Choose (or create) a design library above", exact=False).count() == 0,
            )
            check(
                "submit button labeled for the selected model deliverable",
                submit_btn.inner_text() == "Generate RNM model",
                submit_btn.inner_text(),
            )
            page.screenshot(path=f"{AUDITS}/2026-08-28-deliverable-model-flow.png", full_page=True)

        finally:
            # Always leave the real settings clean, even on failure.
            try:
                page.request.put(
                    f"{API}/api/settings",
                    data='{"working_dir": "/home/rsarasw1/analog-spec-tool", "libraries_dir": null}',
                    headers={"Content-Type": "application/json"},
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  (cleanup) could not reset settings via API: {exc}")
            browser.close()
            tmp_lib_dir = Path("/home/rsarasw1/analog-spec-tool/libraries/browser_check_tmp_lib")
            if tmp_lib_dir.is_dir():
                shutil.rmtree(tmp_lib_dir)
                print(f"  (cleanup) removed {tmp_lib_dir}")
            if Path(SCRATCH_LIBS).is_dir():
                shutil.rmtree(SCRATCH_LIBS)
                print(f"  (cleanup) removed {SCRATCH_LIBS}")

    print(f"\n{passed} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
