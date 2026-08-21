# Overnight Improvements — analog-spec-tool
**Session: Aug 20 ~10:00 PM → Aug 21 7:00 AM**

Loop: Circuit_Researcher audits the tool's analog-domain content → Analog_Tool_Dev implements fixes → repeat. This doc is the brief summary for morning review; details are in each section.

## Status
- 21:55 — Cycle 1 started: Circuit_Researcher auditing topology catalog, spec schema, feasibility ranges.
- 22:00 — Audit done: 12 findings (2 correctness, 8 gaps, 2 minor). Full detail: `audits/2026-08-20-cycle1-audit.md`. Analog_Tool_Dev now implementing.
- 22:05 — **User request (mid-loop):** add behavioral-model generation (Verilog-A, digital Verilog, real-number models) per sub-block. Circuit_Researcher now writing the feature spec (model content per block class + local simulator verification story); Analog_Tool_Dev implements it as cycle 2.

## Cycle 1 — Domain-content audit → fixes (in progress)
Audit verdict: existing per-topology fields/units/help text are correct (numbers re-verified); remaining problems are missing cross-cutting fields, absent validation, and two correctness bugs.

**Findings, brief** (full detail in `audits/2026-08-20-cycle1-audit.md`):
- **P1-1** Current-mirror form allows DRC-illegal widths (min W in sky130 custom layout is 0.42 µm, form allowed 0.15 µm; derived mirror widths unchecked).
- **P1-2** UI example specs unachievable in sky130 (25 Gb/s NRZ placeholder, 10 GHz PLL example) — replace with ~2.5 Gb/s / 2.5 GHz.
- **P2-3** No temperature field; bandgap tempco spec is unverifiable without a temp sweep.
- **P2-4** No load-capacitance field for voltage-output blocks (comparator, CTLE, VGA, sampler, clock buffer…).
- **P2-5** No plausibility bounds on any spec field — add hard min/max + soft sky130 feasibility warnings.
- **P2-6** Unknown `extra_fields` keys silently discarded (typos = ignored constraints).
- **P2-7** `vdd_v` unconstrained; >1.98 V overstresses 1.8V devices, <1.2 V headroom warning needed.
- **P2-8** Corner list missing skewed corners sf/fs (worst case for offset/symmetry blocks).
- **P2-9** PLL has no jitter spec field (the actual deliverable).
- **P2-10** Catalog missing op-amp/OTA, LDO, DLL/phase-interpolator topologies.
- **P3-11** Serializer rate/width/word-clock consistency unchecked; **P3-12** return-loss sign convention ambiguous.

**Implemented (22:35): all 12 findings, nothing skipped.**
- New validation mechanism: per-field hard `min`/`max` + soft sky130 feasibility warnings defined once in `topologies.py`, enforced in shared Pydantic validators, rendered in the form (hard bounds block submit; warnings show amber inline text but submit).
- Fixes landed: 0.42 µm min device width incl. derived mirror-width check; realistic example specs; `temp_c` end-to-end (incl. bandgap temp-sweep instruction to Circuit_Builder); `cload_ff` on six topologies; unknown spec-key rejection; vdd_v guardrails (>1.98 V error, <1.2 V warning); tt/ff/ss/sf/fs corners; PLL/CDR jitter fields; new topologies `opamp_ota`, `ldo`, `dll_phase_interpolator` (DDR strobe blocks remapped to DLL); serializer rate/width/clock consistency warning; return-loss sign convention fixed. Bonus: `selected_block` was silently dropped by the API models — now an explicit field.
- Verified: backend model tests + live FastAPI curl checks (clean 422s with helpful messages, good specs pass); headless-browser run of the form — 24/24 checks, screenshots in `audits/`. No paid Circuit_Builder runs used.
- Files: `backend/topologies.py`, `backend/main.py`, `backend/circuit_builder.py`, `frontend/src/SpecForm.jsx`, `frontend/src/phyArchitectures.js`.

## Cycle 1.5 — Small follow-ups (done 23:10)
- 422 validation errors now render as readable per-field bullets in the UI (`api.js` `apiErrorMessage()`).
- Soft feasibility warnings are computed server-side at submit, persisted on each run record, and shown in ResultsView (amber box) along with Temperature/Corner; failed runs still show requested conditions.
- Data-rate warn text is per-unit (Gb/s vs GS/s); `custom` topology now accepts free-form spec keys end-to-end (they reach the Circuit_Builder prompt), cataloged topologies stay strict.
- Verified: 17/17 backend tests, stubbed-backend curl checks, 12/12 headless-browser checks (screenshots in `audits/`), oxlint clean, real backend restored.
- Remaining minor: ResearchView doesn't show persisted warnings; `/api/runs` list omits `warnings`; mixed field+cross-field 422s report only the field error; stale `backend/main.py.patch` to confirm/delete. → folded into cycle 2.

## Cycle 2 — Behavioral-model generation (user request)
Feature spec done (23:00): `audits/2026-08-20-behavioral-models-spec.md`. Key decisions:
- **RNM for every block** (SystemVerilog with plain `real` ports, `iverilog -g2012`-compatible — not `wreal`/`nettype`, which Icarus doesn't support). This is the level full-link sims consume.
- **Verilog-A only for continuous-time blocks** (CTLE, VGA, TIA, opamp, bandgap, LDO, mirrors): local path is OpenVAF→OSDI→ngspice 45.2, which supports only the compact-model subset — no events/`transition`/`laplace`, so clocked blocks can't be Verilog-A here. Convergence rules (ddt()-based poles, tanh limiting) baked into the prompt contract.
- **Digital Verilog for bit-level blocks** (comparator/sampler, PLL/CDR/DLL, serdes, driver).
- Fixed deliverable filenames, self-checking testbenches with grep-able pass tokens, `summary.json` "models" status enum (verified / compile_only / generated_unverified / failed), UI checkboxes + results-panel model column.
- **Prerequisites missing on this machine:** `openvaf` (static binary from openvaf.semimod.de — must be an OpenVAF-reloaded build, ngspice 45.2 rejects old OSDI 0.3) and `iverilog` (`sudo apt install iverilog`). Until installed, models land as `generated_unverified` but the feature works end-to-end.

**Implemented (23:20): feature complete.**
- New `backend/models_contract.py`: which model types each topology supports, spec-request validation, runtime toolchain probing, the full Circuit_Builder deliverables contract (fixed filenames, OpenVAF/RNM restriction lists, self-checking testbench pass tokens), and an honesty pass — a model claiming "verified" without its pass token in the sim log is downgraded to "failed".
- Backend: `models` request field (422 on inapplicable types), `GET /api/toolchain`, model files/statuses served per run. Frontend: model-type checkboxes (auto-enabled per topology, warns when a verifier tool is missing), ResultsView models card (status badges, source/testbench/log viewers, download links).
- **Real end-to-end Verilog-A proof**: fixture model compiled with openvaf, loaded into ngspice 45.2 via `pre_osdi`, testbench printed its pass token. (Exposed and fixed an OSDI `.model`-card gotcha, now baked into the prompt contract.)
- Tools: `openvaf` (OpenVAF-Reloaded v24.0.1) installed user-space under `tools/`. **`iverilog` needs sudo → user action in the morning: `sudo apt install iverilog`** (until then Verilog/RNM models are marked generated_unverified, with UI warning).
- Cycle-1.5 leftovers folded in: ResearchView warnings box, `warnings` in `/api/runs`, stale `main.py.patch` deleted.
- Verified: 43 backend unit checks, fixture run through the real API, 21 headless-browser checks (screenshots in `audits/`), oxlint clean, real backend restored.
- Deferred: first real Circuit_Builder run with models enabled (planned later tonight after a domain audit of the contract).
- **Start-screen change (direct request to the dev agent, 23:35):** the form now opens with "Select a topology..." and keeps all spec fields/submit hidden until a topology is chosen or a diagram block is clicked. ⚠️ **Please confirm at review:** the request also said "I do want starting screen to show current mirror specs", which contradicts "keep specs empty until user clicks a block" — the agent implemented the empty-until-selected behavior. If you instead wanted current-mirror pre-selected with blank values, that's a small tweak.

## Cycle 3 — Models-contract domain audit → fixes + first real run
Audit done (23:45): `audits/2026-08-21-cycle3-models-audit.md`. Implementation is faithful to spec (matrix, units, restriction lists all check out), but the contract text itself had errors:
- **P1**: Verilog-A pole idiom mathematically wrong (DC gain −6 dB, pole at 2×wp — obedient builder fails its own TB); mirror `tanh(V/Vc)` is 24% low at compliance vs a 1–5% check (fix: `tanh(3V/Vc)`); PLL RNM jitter rules self-contradictory (accumulating jitter random-walks ~32× sigma vs a ±20% check — locked PLLs need per-edge synchronous injection).
- **P2**: LDO mV/V unit trap; RNM pole exponent units unstated; no watchdog timeout in TBs (stuck model = hung paid run); exact-equality timing checks break on 1 fs quantization; no saturation check (a model missing its clamp still gets "verified").
- **P3**: output_driver should be VA-allowed; PSRR/slew untested; comparator dead-band; various wording/consistency items.
- Auditor also corrected the error's origin in its own cycle-2 spec + knowledge base.
- **Calibration-run recommendation**: `nmos_current_mirror` with veriloga+rnm (cheapest transistor-level design; openvaf installed so VA can reach real "verified"; detailed 6-point checklist for judging the run's output).

_(dev agent hit the session token limit ~00:30 mid-implementation; loop couldn't resume overnight. Resumed 10:17 AM on user's "Continue".)_

**Implemented (10:25 AM): all P1/P2/P3 contract fixes + first real calibration run — SUCCESS.**
- All audit fixes landed in `models_contract.py`; 50/50 contract tests. The fixture flow numerically proves the corrected Verilog-A pole idiom (DC gain 20.0000 dB, f3db 1.0000 MHz) and confirms the old idiom was wrong exactly as predicted (13.98 dB = gain/2).
- **Calibration run** (current mirror, veriloga+rnm, real API, 11.7 min, ~$6.06): all 6 checklist items PASS. Verilog-A model genuinely "verified" (openvaf compile + self-checking ngspice TB, checks unweakened); model parameters reflect achieved values (22.12 µA incl. real +10.6% ratio error), RNM honestly marked generated_unverified pending iverilog. Run filed into `cal_runs/current_mirror` (14 views) — libraries integration merged cleanly, all tests pass.
- **Real tool bug found & fixed**: blanket `Write(/**)` deny rules in `circuit_builder.py` were blocking Circuit_Builder from writing its own run dir (it had been working around via xschem Tcl). Replaced with targeted denies.
- ~~`sudo apt install iverilog`~~ → **done by user 10:40 AM.** Icarus 12.0 confirmed working: the calibration run's RNM model compiles (-g2012, real ports) and its testbench prints RNM_TB_PASS (flatness 0.5%, rolloff 0.762 < 0.8, ratio 2.201 vs 2.212). Cycle-4 agent is refreshing the toolchain cache and upgrading the run's RNM status to verified (log-backed).
- Remaining: mirror legacy prompt lacks a `measured` dict so ResultsView's "SPICE measured" column is n/a for that topology; output_driver's new VA path not yet exercised by a real run.
- Browser-verified 15/15; screenshot `audits/2026-08-21-cycle3-calibration-results.png`.

**Implemented (done): all P1/P2/P3 findings.** 50 contract unit checks (`backend/tests/test_models_contract.py`); corrected pole idiom proven end-to-end in ngspice (20.0000 dB / 1.0000 MHz measured; the old idiom measures 13.98 dB — the audit's exact prediction) via `backend/tests/test_va_fixture.py` + fixtures.
**First real calibration run (89112a768b5b): SUCCESS** — 11.7 min, $6.06. Verilog-A model reached genuine "verified" (openvaf compile + self-checking DC-sweep TB printed VA_TB_PASS with the flatness and rolloff clauses intact); RNM honestly "generated_unverified" (iverilog missing). Model defaults = achieved values (22.1166 uA / 0.9 V compliance), not the ideal 20 uA spec. All 6 audit checklist items PASS (one noted gap: the hand-tuned mirror prompt has no `measured` dict for checks keys to align with). Filed into library `cal_runs/current_mirror` (14 views incl. models + .osdi). 15/15 headless browser checks; screenshot `audits/2026-08-21-cycle3-calibration-results.png`.
**Tool bug found by the run:** the cycle-2 "defense in depth" `Write(/**)`/`Write(~/**)` deny rules blocked ALL Write/Edit (deny beats the relative allow; every absolute path matches `/**`), forcing Circuit_Builder to author every file via the xschem Tcl interpreter. Fixed with targeted denies (tool code dirs, ~/.claude, ~/.volare), verified by a headless `claude -p` probe. Libraries integration re-verified post-merge (backend starts, all tests pass, filing + API + UI banner checked). Full detail: "Implemented (cycle 3)" section of the audit file.
**User action still open:** `sudo apt install iverilog` to unlock Verilog/RNM verification.

## Virtuoso-style libraries (direct user request to the dev agent, done ~00:15)
- New `backend/libraries.py` + `/api/libraries`: Virtuoso-like hierarchy as a directory convention `libraries/<library>/<cell>/<views>` (schematic, symbol, netlist, testbench, PNG, behavioral models, measurements, provenance). xschem has no database — so `libraries/` is appended to each run's `XSCHEM_LIBRARY_PATH`, making filed cells browsable/instantiable from xschem directly.
- Spec form now asks for a target library once a block is chosen (pick existing or create inline; submit disabled until chosen). Successful runs auto-file into the library (`library_filed` on the run record; filing failure never fails the build). ResultsView shows the filed lib/cell.
- Also from the same session: start screen now opens empty (no topology pre-selected) per the earlier request.
- Verified: 15 backend unit checks, live API + 11/11 browser checks, screenshots in `audits/`. Your `d2d_rx`/`d2d_tx` libraries created via the live UI were left untouched.
- Deferred: dedicated library-browser panel in the UI (cells are listed by the API and browsable in xschem meanwhile).
- ⚠️ Note: this work ran concurrently with cycle 3's edits to the same backend — integration will be re-verified when cycle 3 lands.

## Cycle 4 — Schematic sub-window (user request, queued behind cycle 3)
**Implemented (11:00 AM): done, including the iverilog follow-through.**
- Collapsible sticky side panel next to the spec form: shows the highlighted block's schematic PNG. Resolution: filed library cell (matching topology, newest first) → latest successful run's PNG → empty state "No schematic yet — submit a spec for this block first."
- "Open in xschem" button: backend-spawned, detached, cwd = run/cell dir (xschemrc auto-provided), gated on a request-time X-display check (409 + disabled button when headless), deduped so repeat clicks reuse the open window's process.
- iverilog follow-through: `/api/toolchain` reports Icarus 12.0 + real-port probe ok; calibration run's RNM model re-verified for real (`vvp` log saved as `rnm_sim.log`) and upgraded to **verified** through the honesty pass, synced into `cal_runs/current_mirror`; all "generated but NOT verified" UI warnings gone.
- Verified: 20/20 new backend tests (+50/50 contract suite still green), curl checks, display-guard both ways incl. one real xschem launch with sky130 symbols resolving, 15/15 headless-browser checks. Screenshots + write-up: `audits/2026-08-21-cycle4-schematic-subwindow.md`.
- Deferred (minor): custom-topology name matching, window raise on repeat click (needs xdotool), dedupe registry survives only per backend session, per-view browsing stays in ResultsView.

## Cycle 5 — Configurable working directory (user request, done ~11:45 AM)
- One `working_dir` setting (Settings gear in the UI), persisted at `~/.config/analog-spec-tool/settings.json` — outside the working dir so it survives repointing. Default = `~/analog-spec-tool`, so nothing changes until you edit it. The tool derives and auto-creates `libraries/`, `runs/`, `research_runs/` beneath it (shown read-only in the panel).
- Validation on save: `~` expansion, absolute path, create-if-missing with a real write probe, source-tree paths rejected — clear inline errors. Changes apply on the next request, no restart.
- Moving the working dir never moves data: old locations are remembered and searched (runs, libraries, schematic resolution, xschem library paths), with a note in the panel that relocating files is manual. Library picker and the submit button now show exactly where things will land.
- Verified: 35/35 new tests (+ schematics 20/20 and contract 50/50 still green), live curl checks incl. old `cal_runs` mirror still resolving after a root switch, 4-step browser pass with screenshots; defaults restored. Write-up: `audits/2026-08-21-cycle5-configurable-paths.md`.
- Minor notes: same-name libraries at two roots list current-root-first; no pruning UI for stale previous-root entries.

## Final state (11:00 AM Aug 21)
All planned cycles plus all four mid-session user requests are complete: domain-content fixes (cycles 1/1.5), behavioral-model generation with a fully working verify chain for all three model types (cycles 2/3, incl. a passing real calibration run), Virtuoso-style libraries, empty start screen, and the schematic sub-window (cycle 4). Open question for the user: start-screen behavior — empty-until-selected (as implemented) vs. current-mirror pre-selected with blank fields.
