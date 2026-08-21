# Cycle 4 — schematic sub-window + iverilog toolchain completion (2026-08-21)

## Implemented (cycle 4)

### Schematic sub-window
When a block is highlighted (PHY diagram click or topology dropdown), a collapsible,
sticky side panel next to the spec form shows that block's best available schematic.

Resolution (backend/schematics.py, single source of truth, re-used by both endpoints):
1. Filed library cell: any `libraries/<lib>/<cell>/provenance.json` whose `topology`
   matches and whose cell has a rendered PNG view — most recently filed wins.
2. Else the most recent successful run for that topology whose `summary.schematic_png`
   still exists on disk. Runs from before the topology field existed count as
   `nmos_current_mirror` (the only flow back then).
3. Else the defined empty state: "No schematic yet — submit a spec for this block first."

Endpoints (backend/main.py):
- `GET /api/schematic/resolve/{topology}` — resolution result + `png_url` into the
  existing run/library file-serving endpoints + `xschem: {available, display}`
  (X-display availability re-checked on every call).
- `POST /api/schematic/open` — re-resolves server-side, spawns interactive xschem
  detached (`start_new_session`, never `-x`) with cwd = the run dir (per-run xschemrc)
  or the library cell dir (xschemrc auto-written on first launch, PDK + libraries on
  XSCHEM_LIBRARY_PATH). One live process per schematic file (`already_open` reuse);
  409 when no display, 404 with the empty-state message for un-run blocks.

Frontend: `SchematicPanel.jsx` (collapsible; PNG default view, source line naming the
filed cell/run, "Open in xschem" disabled with tooltip when no display), wired through
`SpecForm` -> `App` arch-state (now carries topology + label) so the panel follows both
selection paths. Files: frontend/src/SchematicPanel.jsx, App.jsx, SpecForm.jsx, api.js,
App.css.

### iverilog addendum (Icarus 12.0 installed by the user)
- Toolchain probe: backend restart refreshed the in-memory cache; the disk-cached
  real-port probe (tools/realport_probe.json) ran post-install and reports "ok".
  `GET /api/toolchain` now: iverilog available (/usr/bin/iverilog), realport_probe ok.
- Calibration run 89112a768b5b RNM re-verification — actually executed, not hand-set:
  `iverilog -g2012` compile clean, `vvp` printed `RNM_TB_PASS`
  (worst flatness above compliance 0.00494525; iout@vc/3 = 16.8439 µA = 0.762 of the
  22.1166 µA target, confirming rolloff; ratio 2.20072 vs 2.21166 expected).
  New `rnm_sim.log` saved in the run dir (with a header noting the re-run and the old
  "not installed" placeholder it replaces); summary.json + status.json updated with
  status `verified` and measured check values parsed from the log; the update was
  gated through `models_contract.check_models_in_summary` (honesty pass) which left the
  claim standing; `rnm_sim.log`/`summary.json` synced into
  libraries/cal_runs/current_mirror/ and the re-verification noted in its
  provenance.json.

## Verification evidence
- Unit: backend/tests/test_schematics.py — 20 checks (library-over-run preference,
  latest-successful-run fallback, failed/missing-PNG runs skipped, legacy-run mapping,
  testbench .sch never picked, empty state, topology-name validation, display guard
  incl. fallback-display and dead-socket cases). All pass; test_models_contract.py
  still 50/50.
- curl against the live backend: resolve(nmos_current_mirror) -> library cell
  cal_runs/current_mirror (run 89112a768b5b), PNG serves (200, image/png);
  resolve(pll) and resolve(ctle, only failed runs) -> found=false + empty-state
  message; toolchain and run-record endpoints show iverilog available / rnm verified.
- DISPLAY guard both ways: mocked no-display environment -> resolve reports
  xschem.available=false with reason, open -> HTTP 409 (scratchpad endpoint test);
  real display :2 -> launched xschem once via the endpoint (PID returned, PDK log
  clean), second click returned `already_open` with the same PID, killed process
  freed the slot and relaunch worked; no xschem processes left afterwards.
- Headless browser (playwright, 15 checks all pass): no panel before any selection;
  mirror selection renders the calibration PNG from the library endpoint
  (naturalWidth > 0), source line names cell + run, xschem button enabled; PLL block
  click shows the empty state; collapse/expand works; no JS console errors; with
  Verilog-A + RNM checked, zero "generated but NOT verified" warnings.
  Screenshots: 2026-08-21-cycle4-subwindow-mirror.png,
  2026-08-21-cycle4-subwindow-empty-pll.png, 2026-08-21-cycle4-models-no-warning.png.

## Deferred
- "custom" topology resolution matches the literal `custom` category, not the
  free-text custom_topology name — two different custom blocks would collide; refine
  when a second custom block actually exists.
- The already-open xschem window is not raised/focused on a repeat click (the click is
  acknowledged with "already open"); window focusing would need xdotool/wmctrl.
- The per-schematic process registry is in-memory; after a backend restart a repeat
  click can open a second window for the same cell (bounded: one per restart).
- Sub-window shows the block schematic view only; per-view browsing (symbol, netlist,
  testbench) stays in ResultsView / the library, not duplicated here.
