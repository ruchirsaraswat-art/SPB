# 2026-08-21 — Single-deliverable run modes ("Generate" dropdown)

User request: a Generate pull-down in the spec form producing ONLY a chosen
deliverable for the selected block, instead of the full design+simulate+models
flow. Modes: schematic generation only, symbol generation, Verilog-A model,
digital (System)Verilog model, real-number model (RNM).

## What was built

### Backend
- `RunSpec.deliverable: Literal["full","schematic_only","symbol","veriloga","verilog","rnm"]`
  (default `"full"` — pre-feature behavior byte-identical, verified by test).
  Validation (`backend/main.py`):
  - model-only deliverables go through the same `MODEL_APPLICABILITY` matrix
    as the checkboxes → 422 with the identical reason strings;
  - `models=[...]` combined with a non-full deliverable → 422 (contradictory).
- Prompt branches (`backend/circuit_builder.py`):
  - `schematic_only`: design + hand-write .sch + headless netlist + rendered
    PNG; explicitly forbids the verification sim suite and models; allows one
    quick DC `.op` sanity check (`dc_sanity.log`, `dc_check_note`).
  - `symbol`: only `<cell>.sym`. When the chosen library has a filed cell for
    the topology, its schematic view is copied into the run dir
    (`_prepare_symbol_inputs`) and pins are derived from it; else from the
    spec ("ports"/"port_source" recorded in summary.json). Verified cheaply by
    netlisting a wrapper `symbol_check.sch`. No sims.
  - `veriloga`/`verilog`/`rnm`: model-only prompt = spec context +
    `build_models_prompt_section(..., standalone=True)` — the per-type
    contract text (filenames, OpenVAF/RNM/Verilog restrictions, verification
    commands, pass tokens, honesty semantics) is shared verbatim with the
    full flow; only the framing differs (params default to spec targets +
    documented judgment values, since no transistor-level measurements exist).
- Result evaluation extracted to `evaluate_summary()` (unit-testable):
  per-mode required artifacts (PNG for full/schematic_only, .sym for symbol),
  models honesty pass for exactly the requested type; on model-only runs a
  failed model fails the RUN (on full runs it still only downgrades the badge).
- Cheapness reflected in metadata + limits: run records (and GET /api/runs)
  carry top-level `deliverable` + `light_run` (symbol + model modes);
  per-deliverable timeouts (`timeout_for`): symbol 600 s, model 1200 s,
  schematic_only 1800 s, full unchanged 2400 s.
- Library filing (`backend/libraries.py`): runs without a schematic of their
  own (`symbol`/model-only) file into the EXISTING cell for the topology in
  the chosen library (`find_cell_for_topology`), adding/replacing just their
  view files; provenance is updated to the new run but its `views` list
  merges surviving earlier views (schematic etc. stay recorded); `deliverable`
  recorded in provenance; `symbol_file`/`dc_log_file` added to artifact keys.

### Frontend
- Generate dropdown (`SpecForm.jsx`) next to the submit button; ineligible
  model options disabled with the applicability reason (option title + a
  compact inline note); model checkboxes render only for `full` (hidden
  otherwise — no contradictory requests possible); a model-only deliverable
  auto-resets to `full` when a topology change makes it inapplicable; submit
  button text per mode ("Generate schematic", "Generate RNM model", ...);
  per-mode hint incl. the missing-toolchain "generated but unverified" warning.
- `App.jsx`: symbol/model-only submissions skip the Circuit_Researcher step
  and launch the build directly (nothing to research; cheap runs).
- `ResultsView.jsx`: deliverable badge ("Symbol only · light run", ...);
  per-mode sections — symbol runs get a symbol block (file link, port list,
  pin source) and NO schematic/metrics/models sections; model-only runs get
  conditions+targets and the models table, no schematic/measured sections;
  schematic_only shows the schematic + DC-sanity note without measured
  metrics; failures surface plainly (badge + reason).

## Schematic-visibility side check (user report: "could not see schematic")

- Reproduced systematically across viewports with the current-mirror topology
  selected. At 1280/1366/1600/1920 px the sub-window renders the cal_runs PNG
  correctly (naturalWidth 900, visible in the initial viewport) — no
  regression from the ArchChatSidebar flex-row change at desktop widths.
- Actual failure mode found: at viewport widths <= 1100 CSS px (half-screen
  window, or e.g. 125% browser zoom on a 1366 px laptop) the `.split-panels`
  media query stacks the layout and the schematic panel landed ~2400 px down,
  BELOW the tall spec form — invisible without knowing to scroll, while the
  resolve API works. Fix: in the stacked layout the schematic panel now
  orders FIRST (it is collapsible) and drops its sticky positioning; verified
  in-viewport at 1024 and 1100 px after the fix
  (2026-08-21-deliverable-narrow-schematic-first.png).

## Verification evidence

- Unit: `backend/tests/test_deliverables.py` — 48 checks, all pass (prompt
  branches incl. full-prompt byte-identity, standalone/full contract-text
  sharing, applicability 422s through both RunSpec and the HTTP layer,
  evaluate_summary per mode, single-deliverable library filing + provenance
  merge, symbol input prep, timeouts). All pre-existing suites still pass
  (models_contract 50, schematics 20, settings 35, arch_chat 24, VA fixture).
- Stub-run fixtures through the API for each mode (schematic_only, symbol,
  rnm success, rnm failure) rendered via `?run=` deep links; headless-browser
  pass (`ui_deliverables.py`) — 45 checks, all pass: dropdown behavior,
  disabled options + reasons, checkbox hiding, submit labels, mode reset on
  topology change, per-mode ResultsView sections, failure surfacing, and the
  schematic sub-window. Screenshots:
  2026-08-21-deliverable-dropdown-rnm.png,
  -schematic-subwindow.png, -results-schonly.png, -results-symbol.png,
  -results-rnm.png, -results-rnm-failed.png. Stub run dirs removed afterward.
- Real smoke run (authorized, one): run `9b8b7de8b992` — rnm model-only for
  the 10 uA 2:1 nmos_current_mirror into library `cal_runs`. Result: success
  in 158 s at $1.08 (vs 15-30+ min full runs); no schematic/netlist files in
  the run dir (scope respected); model `verified` via iverilog/vvp with
  RNM_TB_PASS in rnm_sim.log (cross-checked on disk); checks table
  target-vs-model within 0.01% (iout 19.999045/20 uA); filed into
  cal_runs/current_mirror updating only the rnm views + provenance (earlier
  schematic/VA views preserved in the merged provenance). Browser-verified:
  2026-08-21-deliverable-real-rnm-smoke.png.

## Addendum (same day): three follow-up user requests

### 1. Generate menu under the Architecture pane (deliverable="arch_model")
Behavioral model of the ENTIRE displayed block diagram, or of the blocks the
user ticks. New RunSpec fields `architecture` ({blocks, edges}, the arch-chat
shape), `arch_blocks` (subset ids, empty = all) and `arch_model_type`
("rnm"|"verilog"). Validation (`models_contract.validate_arch_model_request`):
pads/topology-null blocks auto-skipped, unknown ids / no designable blocks /
a level not applicable to EVERY included block → 422 with the matrix reasons
(RNM is the only level valid for all block types; digital Verilog offered
only for clocked-only subsets). Prompt (`_build_arch_model_prompt` +
`arch_deliverable_files`): one model module per included block (per-topology
guidance paragraphs from models_contract), a top-level module wired per the
diagram edges (documented signal convention), RNM_RULES/VERILOG_RULES once,
end-to-end self-checking TB with ARCH_TB_PASS/ARCH_TB_FAIL tokens, iverilog
verification, fixed filenames. `evaluate_summary` checks all files on disk
and backs a "verified" claim with the pass token; filing copies top +
per-block files into a cell named after the top module. Timeout 1800 s, not
a "light" run. Frontend: `ArchGenerateMenu.jsx` under the diagram (scope
entire/selected with per-block checkboxes, level select with applicability
gating, optional library, skipped-pads note); arch runs skip the research
step; ResultsView gets an "Architecture model" section (top module +
status badge, blocks modeled, per-block/top/TB/log links) and no
metrics/schematic sections.

### 2. Library cell picker (work on an existing cell or generate a new one)
Once a library is selected the form lists its cells (name + topology, with
the cell's existing view kinds shown) plus "+ Generate a new cell". New
RunSpec field `cell` (validated, same charset as library names): filing
targets exactly that cell (overrides schematic-stem naming), symbol runs
read their pin source from it, and full/schematic_only prompts are told to
name the schematic `<cell>.sch` so views land as views OF that cell.
Choosing a cell pulls the form topology from the cell's provenance; manually
changing topology afterwards clears the targeted cell (stale-target guard).

### 3. Schematic window under the chat window (layout)
The schematic sub-window moved out of the form row into the right column,
UNDERNEATH the ArchChatSidebar. The column is sticky and internally
scrollable; the chat is height-capped (55vh) so the schematic actually shows
below it (verified top of panel at y≈549 on a 1100-px-high viewport). This
supersedes the earlier narrow-layout order fix (removed); at <=1100 px the
whole right column stacks below the form as before.

### Addendum verification
- `tests/test_deliverables.py` extended to 67 checks, all pass (arch_model
  validation/422s incl. HTTP layer, prompt fragments, files contract,
  evaluate_summary honesty, arch filing; cell-targeting validation, prompt
  naming, filing override, symbol prep). All other suites still green.
- Second headless-browser suite (`ui_deliverables2.py`, 25 checks, all pass):
  chat+schematic column layout/ordering/PNG render, cell picker (cells
  listed with topology, views hint, topology sync, stale-cell clearing),
  arch generate menu (scope select, verilog gating full-diagram vs
  clocked-subset, disabled-empty-selection, pads note), arch_model stub
  ResultsView. First suite re-run after the layout change: 45/45 still pass.
  Screenshots: 2026-08-21-schematic-under-chat.png, -cell-picker.png,
  -arch-generate-menu.png, -deliverable-results-archmodel.png.
- No real arch_model run executed (not authorized); stub-verified only.

## Addendum 2 (same day): stitched top-level design (deliverable="arch_stitch")

User request: an option for the entire architecture to be stitched together
in the DESIGN (not just behavioral models), asking the user where the
top-level stitch goes and where the individual blocks are placed from.

- Backend: `deliverable="arch_stitch"` + `arch_cell_map` ({block_id:
  "<library>/<cell>"}). Validation (`libraries.validate_stitch_cell_map`,
  shared block normalization via `models_contract.normalize_arch_blocks`):
  every included designable block must map to an existing filed cell WITH a
  symbol view (422 listing all problems at once, with the "Generate ->
  Symbol only" fix hint); a destination library is required. Prompt
  (`_build_arch_stitch_prompt`): one top-level `<cell>.sch` instantiating
  each mapped `<lib>/<cell>/<cell>.sym` (they resolve via the run xschemrc's
  XSCHEM_LIBRARY_PATH - no file copying), wired per the diagram edges with
  real pin names read from the symbols, pads becoming labeled top-level
  pins, left-to-right signal-flow placement; netlist (subckt-call check) +
  rendered PNG; NO simulation and NO block-internal edits. evaluate_summary
  treats it like a schematic deliverable (PNG required); filing lands at the
  user-chosen library/cell destination. Timeout 1200 s.
- Frontend: the arch menu gained an Output select (behavioral model vs
  stitched schematic). Stitch asks the two questions verbatim: "Where should
  the top-level stitch go?" (library + top cell name, default <phy>_top) and
  "Where should each block be placed from?" (per-block dropdown of filed
  cells matching the block's topology that carry a symbol view; blocks with
  no candidate are listed as not-stitchable with the build-symbol-first
  hint and excluded). Generate stays disabled until a destination is chosen.
  ResultsView renders the stitch PNG, a "Blocks stitched" placement list,
  top-level ports, and the netlist artifact.
- Verified: tests extended to 79 checks (validation 422s incl. the
  no-symbol-view case, prompt fragments, evaluate/filing); browser passes -
  10 stitch-menu checks (destination gating, placement dropdown fed by a
  real symbol-bearing cell, not-stitchable listing) and 6 stitch-stub
  ResultsView checks, screenshots 2026-08-21-arch-stitch-menu.png and
  -deliverable-results-archstitch.png. No real stitch run yet (needs a real
  filed symbol; the stub .sym used for testing was removed from cal_runs).

## Addendum 3 (same day): stitch also generates missing symbols

User request: "When generating the top level arch, generate the symbols as
well." Cells mapped in the stitch placement that have a schematic view but
no symbol view are now VALID (previously a 422 telling the user to run
Generate -> Symbol first):
- `validate_stitch_cell_map` accepts .sym OR .sch (neither -> still 422) and
  reports has_symbol per mapping.
- `_prepare_stitch_inputs` copies each symbol-less cell's filed `<cell>.sch`
  into the run dir; the stitch prompt gains a "Symbols to generate FIRST"
  section (pins EXACTLY from the copied schematic's ipin/opin/iopin, same
  contract as the symbol deliverable), instantiates those fresh local .sym
  files in the top level, and records them in summary.json's
  `symbols_generated`.
- `evaluate_summary` requires every claimed generated symbol on disk.
- After a successful run the backend files each generated symbol back into
  its block's OWN library cell (`libraries.file_generated_symbols`, with a
  provenance-views merge + note) - recorded on the run as `symbols_filed`.
- UI: placement candidates now include schematic-only cells, marked
  "symbol will be generated" (option suffix + inline note); the
  not-stitchable warning only applies to cells with neither view.
  ResultsView's stitch section lists the generated symbols and where they
  were filed back.
- Verified: tests extended to 88 checks all passing (relaxed validation,
  neither-view 422, prompt fragments, prep copy/no-copy, eval
  missing-symbol failure, back-filing incl. graceful skip); browser passes
  5 menu checks (cal_runs/current_mirror - a real schematic-only cell -
  offered with the marker) + 2 ResultsView checks. Screenshots:
  2026-08-21-arch-stitch-symbolgen-menu.png,
  -deliverable-results-stitch-symgen.png.

## First real user-driven arch_model runs (observed during development)

While this was being built, two real arch_model runs were launched from the
live UI (not by the dev scripts - they filed into libraries the scripts
never touch): 235e7a00d226 (tx-ffe only -> lpddr_tx) and efcf21dd4f48
(rx-eq only -> serdes_tx), both success, RNM verified, ~$1.10 / ~2.5 min
each. They exposed a naming bug: every subset model filed as cell
"ser_des_arch" (phy-derived), so two different subsets in one library would
overwrite each other. Fixed: subset runs of <= 2 blocks now name the top
module (and cell) after the block ids (e.g. tx_ffe_arch); whole-diagram
runs keep the phy-derived name. Menu labels also pluralize correctly now.

## Deferred / notes

- Symbol and Verilog-A/Verilog model-only modes have not had a real
  (non-stub) Circuit_Builder run yet (only rnm was authorized); their prompts
  and result handling are unit/stub-tested. First real symbol run should
  confirm Circuit_Builder's .sym authoring against the copied filed schematic.
- Run-history labeling: GET /api/runs now carries deliverable/light_run, but
  the app has no run-list UI yet — the labels currently show only on the
  results page. Wire into a history view when one exists.
- "Open in xschem" is not offered for a symbol-only result (only the .sym
  file link); could reuse the library-cell launch path later if wanted.
- ResearchSpec intentionally has no deliverable field: research only precedes
  full/schematic_only builds; symbol/model-only/arch-model submissions skip
  research.
- arch_model awaits its first real run (stub-verified only); when authorized,
  a small clocked-subset run (sampler+cdr, verilog) would be the cheapest
  smoke test.
- The built-in current-mirror full prompt keeps its fixed
  `current_mirror.sch` name; targeting a differently-named existing cell
  with a FULL mirror run files current_mirror-named views into that cell
  (works, but the view filenames won't match the cell name). Generic
  topologies get the exact-name instruction.
- The arch generate menu reads the top-level diagram only; hierarchical
  children (drill-in levels) are not flattened into the composite model yet.
