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

## Hierarchical design libraries (direct user request to the dev agent, done ~00:15)
- New `backend/libraries.py` + `/api/libraries`: hierarchical library/cell/view structure as a directory convention `libraries/<library>/<cell>/<views>` (schematic, symbol, netlist, testbench, PNG, behavioral models, measurements, provenance). xschem has no database — so `libraries/` is appended to each run's `XSCHEM_LIBRARY_PATH`, making filed cells browsable/instantiable from xschem directly.
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

## Cycle 6 — Architecture-chat sidebar (user request, done ~1:30 PM)
- Drawing tab now has a collapsible sidebar (25% width, diagram keeps 75%) for discussing the displayed architecture with a headless Circuit_Researcher-style consultant. Backend: `POST /api/arch_chat` with per-session transcripts persisted under the working dir; each message is a paid invocation (~$0.13–0.20, 20–90 s) with cost shown per reply.
- The consultant can propose **structured architecture patches** (add/remove/rename blocks, set topology, add/remove connections) — validated server-side against the topology catalog (invalid ops shown with per-op errors, never blindly applied). The sidebar renders proposals in plain words with Apply / Dismiss / Undo; Apply mutates the live diagram (with grid-collision handling).
- Modified architectures can be saved as named customs under `<working_dir>/architectures/` and appear in the PHY selector alongside built-ins. Block→click→spec-form contract preserved.
- Split across agents per lane: Analog_Tool_Dev built backend/contract (24 stubbed tests + $0.20 smoke chat), Graphics_Dev built the sidebar (24/24 mocked-network UI checks + one real $0.13 chat: "add a CTLE before the sampler" → valid 4-op patch applied, before/after screenshots). Contract + status: `audits/2026-08-21-arch-chat-feature.md`.

## Cycle 7 — "Generate" deliverable modes + schematic-visibility fix (user request, done ~3:00 PM)
- New Generate dropdown on the spec form: full flow (default, unchanged), schematic only, symbol, Verilog-A / SystemVerilog / RNM model-only. Model-only runs skip transistor-level design and generate+verify just the model via the existing contract (applicability matrix enforced with the same reasons as the checkboxes); symbol runs derive pins from the filed library schematic when one exists. Per-mode timeouts, "light run" labeling, mode-appropriate ResultsView sections, and single-view filing into the existing library cell.
- **Schematic-visibility root cause found**: no regression at desktop widths — but at viewports ≤1100 px (half-screen window / ~125% laptop zoom) the responsive breakpoint stacked the schematic panel ~2400 px below the spec form, effectively invisible. Fixed: in stacked layout the panel now orders first. Verified at 1024/1100/1280–1920 px.
- Verified: 48 new unit checks + all prior suites green, 45-check browser pass over all modes, and one real smoke run (RNM-only current mirror: 158 s, $1.08, model verified via iverilog, filed into `cal_runs/current_mirror` touching only the RNM views). Write-up: `audits/2026-08-21-deliverable-modes.md`.
- Deferred: real smoke runs for symbol/veriloga/verilog modes; run-history list UI; "Open in xschem" for symbol-only results.

**Three follow-ups sent directly to the same agent (done ~4:00 PM):**
- **Architecture-level model generation** (`arch_model`): a Generate menu under the Architecture pane builds one composite behavioral model of the whole diagram (or just ticked blocks) — per-block modules plus a top-level wired per the diagram's connections, with an end-to-end self-checking testbench (`ARCH_TB_PASS`) verified via iverilog. RNM by default (the only level valid for every block type); digital Verilog offered only for clocked-only subsets. Files into a library cell named after the top module.
- **Library cell picker**: choosing a library now lists its cells (topology + which views exist); you can target an existing cell (filing, symbol pin source, and naming honor it) or create a new one; picking a cell syncs the form topology.
- **Layout**: the schematic sub-window moved to the right column directly under the chat (sticky, chat height-capped) so it's always in view — supersedes the earlier ≤1100 px stacking fix.
- **Stitched top-level design (`arch_stitch`, ~5:30 PM):** the Architecture Generate menu can now produce one xschem schematic instantiating each block's filed library *symbol* and wiring them per the diagram. The UI asks exactly two things: destination (library + top cell name) and, per block, which filed cell to place it from (only cells with a `.sym` view qualify; blocks without one show "generate its symbol first"). Pads become labeled top-level pins; output is schematic + netlist + PNG, no simulation. Cell mapping validated server-side in one 422.
- Verified: 79 backend unit checks + four browser suites (45/25/10/6) all green; screenshots in `audits/`. **Two real user-driven arch_model runs from the live UI succeeded** (~$1.10 each, iverilog-verified) and exposed a real bug — subset models all filed under the same cell name, overwriting each other — now fixed (small subsets name the cell after their block ids).
- **Stitch auto-generates missing symbols (~8:30 PM, after a session-limit pause):** blocks whose library cell has a schematic but no symbol are now stitchable — the run derives the `.sym` from the filed schematic first (same contract as the symbol deliverable), uses it in the top level, and back-files it into the block's own cell (`symbols_filed` on the run record; a claimed-but-missing symbol fails the run). UI marks such cells "symbol will be generated". Backend suite now 88 checks, all green; browser checks pass against the real `cal_runs/current_mirror` schematic-only cell.
- Deferred: first real `arch_stitch` run (now fully self-sufficient); real symbol/veriloga/verilog/schematic_only smoke runs; hierarchical diagram levels aren't flattened into arch-wide runs.

## Cycle 8 — Digital architecture + AFE↔digital interface workspace (user request, Aug 23)
- Spec by the new Digital_Microarchitect agent: `audits/2026-08-23-digital-arch-spec.md` (digital block catalog ~20 blocks/PHY-typed, digital diagrams, full interface-definition schema with worked 1.6 Gb/s SerDes RX example, if_chat patch contract, start-screen UX).
- Implemented by Analog_Tool_Dev: `backend/digital_blocks.py` catalog API; arch_chat `view: afe|digital`; `backend/interfaces.py` + `if_chat.py` (signal/clock/CDC/sequence tables, hard-vs-soft validation, 10 patch ops, transcripts, storage under working dir); frontend interface editor with inline validation and Apply/Undo; seeded starter interfaces for all four PHYs; **stacked workbench** (AFE → Digital → Interface, per user's mid-task request; each panel minimizable, one chat sidebar follows focus with per-workspace sessions).
- Verified: 52 new stubbed tests + all suites green, 41 browser checks ($0 via network interception), one real if_chat smoke ($0.29, sound reply, transcript persisted). Screenshots in `audits/`.
- Graphics_Dev rendering done (33/33 browser checks): one shared renderer with a `store`/`variant` prop — AFE view untouched, digital view teal-accented with indigo "boundary IF" nodes (draggable/wireable, never design targets), visual-only selection, per-PHY catalog picker, chat patches re-render live. Screenshots in `audits/`. Open (minor): Save/Load UI for digital architectures; dedicated ddr/hbm diagrams.
- Also created Aug 23: **Digital_Microarchitect** and **Token_Optimizer** agents (user will run the optimizer after heavy sessions).
- **Workbench restructure (user request, approved plan, done Aug 23 PM):** new top-level **PHY architecture** overview panel (`[Line] ⇄ AFE ⇄ IF ⇄ Controller ⇄ [SoC]`, click a block to jump to its workspace); "Digital architecture" renamed **PHY / Controller architecture**; interface panel renamed **AFE ↔ Controller interface** and moved between AFE and Controller (signal-path order). Display-only renames — ids/sessions/storage/API untouched. 48/48 browser checks, vite build + 76 backend tests green.

## Cycle 9 — Firmware section (user request, Aug 23 PM, in progress)
- New **Firmware_Coder** agent created (~/.claude/agents/Firmware_Coder.md): CSR drivers from the tool's interface definitions, init/cal/training sequences in portable C99, gcc host unit tests.
- Spec done by Digital_Microarchitect: `audits/2026-08-23-firmware-section-spec.md` — 13-block firmware catalog (no Verilog models; verification = compile+test), layered firmware diagrams per PHY between Host-API and Controller-CSR anchors, overview gains a dashed "SW" **Firmware** block ([Line] ⇄ AFE ⇄ IF ⇄ Controller ⇄ Firmware ⇄ [SoC]), `fw_chat` workspace, and one codegen action (`fw_generate`: regs header + driver + stub + host tests + Makefile, honesty check = server re-runs gcc -Wall -Werror + tests).
- Meanwhile (also user requests): overview blocks now scroll/expand their target section on click, and the PHY Type/Architecture selector is moving into the overview panel — both in one UX pass.
- Implementation queued behind the UX pass (same files), then Graphics_Dev renders the firmware diagrams.
- UX pass done (27/27): overview clicks scroll/expand their section; PHY selector moved into the overview header (usable while collapsed). Firmware implementation now running (incl. new user request: fresh start opens with only the overview expanded, all other panels minimized).

## Cycle 10 — Controller RTL generation + spec-document intake (user request, Aug 23 PM)
- New **RTL_Coder** agent (~/.claude/agents/RTL_Coder.md): synthesizable Verilog-2005 for a selected block or the stitched controller top, iverilog-verified self-checking TBs, consumes JEDEC/PCIe/UCIe spec documents with section citations.
- Spec done by Digital_Microarchitect: `audits/2026-08-23-rtl-gen-spec.md` — spec-doc intake (per-PHY storage + banner asking "is a specification document available?", metadata-only context for chats, selective-Read for generation runs), `rtl_generate` contract with concrete per-block-class TB criteria (FIFO wrap/stress, aligner lock at every bit offset, CSR A5/5A + W1C race, FSM state+timeout coverage, 512-case codec round-trip), server re-execution honesty checks, and UI (per-block menu + whole-controller button with explicit cost confirm, est. $15–40/30–90 min — to be calibrated by one cheap FIFO block run first).
- **Implemented (Aug 23 evening, after one session-limit pause):** spec-doc intake end-to-end (per-PHY storage, banner with Attach/Paste/No-spec, "Spec docs (N)" manager, capped metadata context into controller/interface/firmware chats, selective-Read for generation runs); `rtl_generate` for block and controller scopes run by RTL_Coder with server re-execution honesty checks (iverilog+vvp rerun by the tool itself, downgrade + `partial` verdict semantics); RTL runs are async + cancellable (deliberate deviation — 90-min HTTP requests aren't workable); Block RTL menu with per-block field mini-form, controller launcher with cost-confirm card; ResultsView RTL section with verdict badges and spec-citation chips.
- Verified: backend suite 117 tests green (incl. a deliberately-broken DUT claiming "verified" that the server re-execution correctly downgrades), 46/46 RTL browser checks, overview/firmware suites unregressed, clean production build.
- **Real calibration run**: ser-des elastic FIFO, depth 16 — success with genuine server re-verification (all six FIFO criteria incl. pointer wrap over 128 words and ±300 ppm stress across 2×20,000 words), 5 honest findings, filed into `libraries/serdes_ctrl/rx_fifo/`. **$4.21 / 8.2 min** → whole-controller UI estimate recalibrated to ~$25–55 / 60–120 min (was $15–40/30–90). Controller-scope runs always require explicit user confirmation.

## Token_Optimizer audit (Aug 23 evening) — ranked findings, advisory
1. **Model routing** (~$3–4/day): every headless run uses fable-5; route fw_generate, the three chats, and model-only runs to Sonnet (fw $2.79→~$0.7; arch_model $1.1→~$0.3). Keep fable-5 for full builds + controller RTL.
2. **Fail-fast** ($1–3/bad run): one run burned $0.99 before dying on a write-permission error; two more $1.97 on unparseable summary.json. Add a preflight write check + "stop immediately if you can't write" prompt line + restate the summary schema at prompt end.
3. **Split dual-model calibration runs** (~$2/cycle): the $6.06 cycle-3 run re-read its whole transcript each phase; build + scoped model-only runs ≈ $4.1.
4. **Book aborted-run costs + cost caps**: ~$2.4 of failed-run spend untracked (parse last cost event from the stream on failure); add an incremental ~$10 cost abort to controller RTL (wedged run could hit ~$45 at observed $0.51/min); trim build timeout 2400→1800 s.
5. **Fleet frontmatter**: all 9 charters `model: inherit`; Sonnet candidates Graphics_Dev, Interview_Prep, Firmware_Coder; add one lean-constraint line to the charters lacking any.
6. **Scoped verification**: run only the browser suite matching the touched panel per pass; full sweep once per session (~30–75k agent tokens/day).

**Applied (Aug 23 evening, user-approved "apply everything"):** single model-routing table (`backend/invocation.py`) sending chats/fw_generate/model-only runs to Sonnet via explicit `--model` (fable-5 kept for full builds, stitch, RTL); preflight write-check aborting at $0 + prompt hardening; failed/cancelled runs now book their real cost from the stream; controller-RTL incremental cost cap (default $10, overridable) with honest abort status; build timeout 2400→1800 s; split-run cost hint in the form. Charter side: Graphics_Dev/Interview_Prep/Firmware_Coder → `model: sonnet`, lean-constraint lines added fleet-wide. Verified: 141 backend tests green (24 new), scoped 7/7 browser check only on the touched surface, zero paid runs. Doc: `audits/2026-08-23-token-optimizations.md`.

## Final state (11:00 AM Aug 21)
All planned cycles plus all four mid-session user requests are complete: domain-content fixes (cycles 1/1.5), behavioral-model generation with a fully working verify chain for all three model types (cycles 2/3, incl. a passing real calibration run), hierarchical design libraries, empty start screen, and the schematic sub-window (cycle 4). Open question for the user: start-screen behavior — empty-until-selected (as implemented) vs. current-mirror pre-selected with blank fields.
