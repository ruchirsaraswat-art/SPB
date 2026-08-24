# Cycle 5 — configurable working directory (no more hardcoded data paths)

User request (as amended mid-cycle): instead of three separate path settings,
the tool asks for ONE working directory and creates its sub-trees itself
(`libraries/`, `runs/`, `research_runs/`) under it. `audits/` stays
project-side.

## Implemented (cycle 5)

### Design chosen
- **Single `working_dir` setting**, persisted at
  `~/.config/analog-spec-tool/settings.json` (XDG-aware; deliberately outside
  the working dir so the setting survives pointing the working dir elsewhere;
  `ANALOG_SPEC_TOOL_SETTINGS` env var overrides it for tests). Default =
  `/home/rsarasw1/analog-spec-tool` (the checkout root, i.e. the pre-cycle-5
  hardcoded location), so out of the box nothing changes.
- New module `backend/settings.py` is the single owner of path logic.
  Internals are keyed by **derived sub-roots** (`libraries_root()`,
  `runs_root()`, `research_root()`, created on demand), so a future split
  back into three independent settings only touches that module.
- **Request-time resolution everywhere**: every accessor re-reads the
  settings file, and no module keeps a path constant anymore
  (`main.py` RUNS_DIR/RESEARCH_DIR, `libraries.py` LIBS_DIR and
  `schematics.py` RUNS_DIR/LIBS_DIR are gone). A saved change applies to the
  very next request — no backend restart.
- **Existing data is never moved.** On change, the old working dir is pushed
  onto `previous_working_dirs` (newest-config first). All read paths search
  current-then-previous roots: library listing (`list_libraries`), library
  file serving, run/research directory lookup (`_find_run_dir` /
  `_find_research_dir`), run-registry loading (rescanned on every settings
  PUT, so runs already sitting at a newly-pointed location appear without a
  restart), schematic resolution for the sub-window, the per-run `xschemrc`
  `XSCHEM_LIBRARY_PATH` (all roots appended, so old filed cells stay
  instantiable), and Circuit_Builder's write-deny list (all libraries roots
  stay deny-listed).
- Validation on PUT: `~` expanded, absolute required after expansion,
  rejected inside `backend/`/`frontend/` source trees, created if missing,
  writability proven with a real probe file; clear 422 messages.
- API: `GET /api/settings` (working dir + using-default flag + derived
  sub-paths + which previous locations still hold data + settings-file path),
  `PUT /api/settings` (`{"working_dir": ...}`).

### Frontend
- `SettingsPanel.jsx` + gear button in the header: working-dir edit + Save
  (with Revert), inline backend validation errors, "Using the default
  location" hint, derived sub-paths shown read-only, previous locations
  listed with what data they hold and an explicit "changing the working
  directory never moves files" note.
- Library picker hint now shows the full resolved libraries root
  (`<root>/<library>/<cell>/`); the submit area shows the runs root
  (`<root>/<run_id>/`) so it's obvious where a run will land. Both update
  live when settings are saved (settings state lives in `App.jsx` and is
  passed down).

### Files touched
- `backend/settings.py` (new), `backend/main.py`, `backend/libraries.py`,
  `backend/schematics.py`, `backend/circuit_builder.py`
- `backend/tests/test_settings.py` (new, 35 checks)
- `frontend/src/SettingsPanel.jsx` (new), `frontend/src/App.jsx`,
  `frontend/src/SpecForm.jsx`, `frontend/src/api.js`, `frontend/src/App.css`

### Verification (no Circuit_Builder runs triggered)
- **Unit tests**: `backend/tests/test_settings.py` — 35 checks covering
  validation (tilde expansion, relative/source-tree/unwritable/file-in-path
  rejection, create-if-missing), persistence + previous-roots bookkeeping
  (newest-first, dedup, switch-back), request-time resolution with no module
  reload, previous-roots search for libraries/runs/schematic resolution,
  registry rescan, xschemrc + deny-list covering all roots. Existing suites
  still green: `test_schematics.py` (20), `test_models_contract.py` (50).
- **curl** against the live backend: PUT rejected relative and
  inside-`backend/` paths with clear 422s; PUT to a scratchpad dir applied
  immediately (derived paths + previous-location report correct); POST
  /api/libraries created `scratch_lib` under the scratch root with no
  restart; GET /api/libraries listed the scratch-root library alongside all
  old-root libraries; GET /api/schematic/resolve/nmos_current_mirror still
  resolved the old `cal_runs/current_mirror` cell at the previous root, and
  its PNG served (200, image/png) through the root-agnostic library file
  endpoint; old runs stayed listed and their files served.
- **Headless browser** (Playwright/Chromium on the real dev servers), 4
  passes, screenshots in this directory:
  - `2026-08-21-cycle5-settings-panel.png` — panel open on the scratch
    working dir: derived paths, previous-location data report, never-moves
    note.
  - `2026-08-21-cycle5-settings-validation-error.png` — inline 422 from the
    backend for a relative path.
  - `2026-08-21-cycle5-library-picker-roots.png` — library picker showing
    the scratch libraries root, submit area showing the scratch runs root,
    and the schematic sub-window rendering the old-root `cal_runs` mirror
    cell.
  - `2026-08-21-cycle5-settings-restored-default.png` — defaults restored
    via the panel's own edit+save ("Using the default location" hint), with
    the scratch dir reported under previous locations; picker/run hints
    updated live.
- **Restored state**: settings file left at
  `{"working_dir": "/home/rsarasw1/analog-spec-tool",
  "previous_working_dirs": []}`; live API confirmed back on the standard
  locations with the full library list and mirror resolution from
  `libraries/cal_runs/`. Backend (uvicorn :8000, new code) and frontend
  (vite :5173) left running.

### Deferred / notes
- If the same library name exists at both the current and a previous root,
  the current root shadows the old one in listings (first-root-wins);
  per-file serving falls back across roots. Consolidation is a manual file
  move, as the UI states.
- `previous_working_dirs` entries pointing at deleted directories are
  harmlessly skipped at read time; no pruning UI (not needed yet).
- oxlint: the new `SettingsPanel` carries the same pre-existing
  `set-state-in-effect` warning pattern as LogPanel/SchematicPanel/
  ArchitectureDiagram; no errors.

### Addendum (2026-08-22, user request): removed "Virtuoso" branding
Wording sweep only: all references to the Cadence product name "Virtuoso"
(comments, docstrings, docs, one UI label) replaced with neutral phrasing
("hierarchical library/cell/view", "design library"). No directories, API
routes, functions, or JSON keys renamed (none contained the name). Files
touched: backend/main.py, backend/libraries.py, backend/circuit_builder.py,
frontend/src/SpecForm.jsx, frontend/src/App.css, frontend/src/api.js,
IMPROVEMENTS-2026-08-21.md, audits/2026-08-20-behavioral-models-spec.md,
libraries/cal_runs/current_mirror/xschemrc (generated file, text comment
only). Visible change: library-picker label on the spec form now reads
"Design library (the built cell and its views are filed here)".
