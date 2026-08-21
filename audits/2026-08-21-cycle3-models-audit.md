# Domain audit: behavioral-model generation (2026-08-21, overnight cycle 3)
Auditor: Circuit_Researcher agent. Scope: `backend/models_contract.py` (applicability matrix, prompt contract, restriction lists, per-topology guidance, honesty pass), models wiring in `backend/circuit_builder.py`, cross-checked against `backend/topologies.py` field names and the cycle-2 feature spec.

Overall verdict: the implementation is faithful to the feature spec, the applicability matrix has no wrongly-allowed entries, every spec-field name referenced in the guidance tables exists in `TOPOLOGY_FIELDS` with the right units, the OpenVAF forbidden-construct list matches the verified toolchain facts, the pass-token strings have no substring collisions, and the honesty pass's assumptions (token-in-named-log, per-type downgrade) are sound. However, **two formulas quoted verbatim in the contract are mathematically wrong** (one originated in the cycle-2 feature spec itself, i.e. my own earlier output, and was copied faithfully), and one pair of RNM rules is self-contradictory — all three would make a *correctly-obedient* Circuit_Builder fail its own testbench and burn debug iterations. Those are the P1s.

## P1 — contract math errors (a builder that follows the text verbatim fails its own TB)

**1. The Verilog-A pole idiom in `VA_RESTRICTIONS` is algebraically wrong (gain halved, pole doubled).**
- WHAT: `models_contract.py` `VA_RESTRICTIONS` prescribes, as "the standard idiom":
  `V(int) <+ V(in)*gain - V(int) - ddt(V(int))/wp;  V(out) <+ V(int);`
  Solving the contribution equation V(int) = gain·V(in) − V(int) − (s/wp)·V(int) gives H(s) = gain/(2 + s/wp): **DC gain is gain/2 (−6 dB) and the pole sits at 2·wp**. Every VA TB in the guidance checks DC gain within 1–2 dB and pole within 20%, so a builder using the idiom exactly as written fails both checks on every filtered block (ctle, vga, tia, buffer, opamp, ldo) and has to debug the tool's own reference formula. (The `- V(int)` term belongs to the *current*-contribution/KCL form of this idiom, not the voltage form — that's how the error crept into the cycle-2 spec and my knowledge base; both are being corrected.)
- FIX (edit the string in `VA_RESTRICTIONS`; no analog judgment needed): replace the idiom with the current-contribution form, which is unambiguous and definitely compiles under OpenVAF:
  `I(int) <+ gain*V(in) - V(int) - ddt(V(int))/wp;  V(out) <+ V(int);`
  (internal node `int` has only these contributions; the `-V(int)` term acts as a 1-ohm normalizing conductance, giving exactly V(int)·(1+s/wp) = gain·V(in)). The equivalent implicit voltage form `V(int) <+ gain*V(in) - ddt(V(int))/wp;` (NO `- V(int)` term) is also correct. Update the "zero" parenthetical unchanged — it still holds. Also apply the same correction wherever the idiom was copied (cycle-2 spec section b1 if anyone still reads it).

**2. The current-mirror `tanh` compliance formula cannot meet its own pass criterion.**
- WHAT: `_VA_GUIDANCE["bias_current_mirror"]` / `["nmos_current_mirror"]` prescribe `I(out) <+ Iout_target * tanh(V(out)/compliance)` and the TB check "Iout within the matching % above the compliance voltage". But tanh(1) = 0.762 — the model current is **24% low at V(out) = compliance_voltage_v**, and still 3.6% low at 2× compliance, while `current_matching_accuracy_pct` is typically 1–5%. A builder implementing the formula as written fails the prescribed check and must silently weaken either the formula or the check. This matters doubly because the mirror is the recommended first calibration run (see end of this audit).
- FIX (edit both guidance strings): change the formula to `I(out) <+ Iout_target * tanh(3*V(out)/Vcompliance)` (tanh(3) = 0.995, i.e. within 0.5% at the compliance voltage — passes any matching spec ≥ 1%) and state the check explicitly: "DC sweep of V(out) from 0 to VDD: Iout within current_matching_accuracy_pct of target for all V(out) >= compliance_voltage_v, and Iout < 80% of target at V(out) = compliance/3 (confirms the rolloff exists)". The second clause prevents a degenerate always-on current source from passing.

**3. RNM PLL/CDR jitter rules are self-contradictory: "accumulating jitter for oscillators" + "edge-time stddev within 20% of rms_jitter_ps" cannot both hold.**
- WHAT: `RNM_RULES` says "accumulating jitter for oscillators: add the sample per period" — and a PLL is an oscillator, so an obedient builder accumulates per-period Gaussian samples into the phase. But accumulated (random-walk) edge deviation grows as sqrt(N) without bound, so the `phase_loop` TB check "measured edge-time stddev within 20% of the rms jitter parameter over >= 1000 edges" **fails on the correctly-built model** (stddev at edge 1000 is ~sqrt(1000) ≈ 32× the per-period sigma). The check only passes with *synchronous* (independent per-edge) injection.
- FIX (edit `RNM_RULES` and `_RNM_GUIDANCE["phase_loop"]`): for v1, prescribe synchronous injection explicitly: "PLL/CDR outputs (locked loops): perturb each output edge INDEPENDENTLY by a Gaussian sample of sigma = rms_jitter_ps around the loop-corrected ideal edge time — this directly represents the *integrated* rms jitter the spec field means, and its measured stddev is stationary. Accumulating (per-period-summed) jitter is only for free-running oscillators, none of which are in the v1 catalog — do not use it." Keep the TB check as-is; it is then consistent (and with >= 1000 edges the stddev estimator's own uncertainty is ~2%, well inside the 20% tolerance).

## P2 — checks that mis-fire or let a wrong model pass

**4. LDO dropout unit trap: `V(vdd) - dropout_mv` mixes volts and millivolts.**
- WHAT: `_VA_GUIDANCE["ldo"]` says "vout = smooth-min(output_voltage_v, V(vdd) - dropout_mv)" and `_RNM_GUIDANCE["reference"]` says "vout = min(vset, vin - dropout)". The spec field is in **mV** (e.g. 200); node voltages are in volts (1.8). Implemented literally this yields −198 V. The decision-class guidance already does this right (`offset_mv/1000.0`); the LDO text doesn't.
- FIX: write the conversion into both strings: `V(vdd) - dropout_mv/1000.0` (VA) and `vin - dropout_mv/1000.0` (RNM).

**5. RNM one-pole update formula has unstated units in the exponent — the classic silent-failure path.**
- WHAT: `RNM_RULES` prescribes `y <= y + (1.0-$exp(-TS*w_pole))*(x-y)` but `TS` is naturally in ps (it's the `always #TS` tick under a 1ps/1fs timescale) while `w_pole` is rad/s. Plugging TS in ps gives an exponent of ~1e9·wrong — `$exp` overflows to 0 update or saturates to instant response; either way the "response at pole frequency" TB check fails confusingly, or worse, marginally passes at the wrong pole.
- FIX: make the string explicit: "`y <= y + (1.0 - $exp(-TS_s*wp))*(x - y)` where `TS_s = TS_ps * 1e-12` (the tick in SECONDS) and `wp = 2*pi*bandwidth_ghz*1e9` (rad/s). Do the conversion once into local reals; never put a ps quantity directly in the exponent."

**6. No watchdog-timeout requirement for Verilog/RNM testbenches — a dead model hangs the whole run instead of failing.**
- WHAT: Several digital/RNM TB checks wait on output edges ("measure the output period over 100 cycles", loopback tests). If the model's clock is stuck (e.g. a `#0` from a bad parameter formula), `vvp` produces no events past the stall and can run forever — no `V_TB_FAIL`, no exit — until `circuit_builder.py`'s 2400 s run timeout kills the *entire paid run*. The honesty pass then can't distinguish "hung" from anything else.
- FIX: add one bullet to both `VERILOG_RULES` and `RNM_RULES`: "Every testbench must contain a global watchdog: `initial begin #WATCHDOG_PS; $display(\"V_TB_FAIL watchdog timeout\"); $finish; end` (token per level), with WATCHDOG_PS ~ 20x the expected test duration. A silent hang is never an acceptable failure mode."

**7. Exact-equality timing checks will fail from 1 fs quantization on non-round frequencies.**
- WHAT: `_V_GUIDANCE["pll"]` says "measure the output period over 100 cycles == 1/output_freq_ghz"; `dll_phase_interpolator` says "measure delay == code*step"; comparator says "measured delay equals the parameter". Under `timescale 1ps/1fs` a 3 GHz half-period (166.6667 ps) quantizes to 166.667 ps, so the reconstructed period is 333.334 ps ≠ 333.333... — literal `==` fails on any frequency whose period is not an exact multiple of 2 fs, purely from simulator quantization, not model error.
- FIX: replace every `==` in the guidance strings with an explicit tolerance: periods/delays "within 0.1% or +/- 2 fs, whichever is larger". (The RNM phase_loop check already does this right with "within 0.1%".)

**8. No saturation/clipping check anywhere — a model that omits its required clamp passes every TB.**
- WHAT: The VA guidance *requires* tanh output clamps (ctle/vga/opamp) and the RNM amp class requires saturation, but no TB check exercises them: all amp checks are small-signal (AC gain, pole location). A model with the clamp missing entirely — wrong by the contract's own definition — sails through "verified". This is exactly the "passes on a trivially wrong model" class of failure.
- FIX: add one clause to the amp-class checks (VA: ctle/vga/opamp_ota rows; RNM: "amp" row): "Overdrive check: drive the input with 3x the amplitude that would produce full output swing at the given gain; verify |V(out)| stays <= output_swing_vpp/2 within 5% (VA: transient with a large sine; RNM: large-amplitude samples). A model without its clamp must fail this." For VA this needs a small `tran` in addition to the `ac` — both run in the same `.control` script.

## P3 — worth strengthening, not blocking

**9. `output_driver` is denied Verilog-A with an inaccurate reason — and VA is the only level that can capture its impedance spec.**
- WHAT: The disabled-tooltip says "clocked/event-driven behavior can't be expressed", but a driver output stage is not clocked — `V(out) <+ (swing/2)*tanh(k*V(in))` behind a series `output_impedance_ohm` is squarely inside the OpenVAF subset, driven by an ordinary PULSE source in SPICE. Meanwhile RNM is directional and *cannot* represent `output_impedance_ohm`/return-loss at all, so today no model level captures the driver's second headline spec.
- FIX: either move `output_driver` into the VA+Verilog+RNM set (guidance: "tanh limiting stage, output swing into a matched termination, series rout = output_impedance_ohm; TB: DC sweep measures small-signal rout within 10%, transient measures swing into 50 ohm within 5%") — note it would be the only block with all three levels, so also update the "at most one other level" comment — or at minimum correct the tooltip text so the denial reads as a deliberate scope choice ("driver dynamics live in its termination interaction; v1 models it at bit/RNM level only").

**10. Bandgap/LDO VA models are required to include a PSRR term the TB never exercises.**
- WHAT: `_VA_GUIDANCE` requires a vdd-sensitivity term (bandgap) and supply-ripple attenuation (ldo), but the TB checks are temperature/Vin-regulation sweeps only. The PSRR term can be absent or 60 dB wrong and still "verified".
- FIX: add "DC vdd sweep: dVref/dVdd <= 10^(-psrr_db/20) within 2x" (bandgap) and "AC: V(out)/V(vdd ripple) at 1 kHz within 6 dB of -psrr_db_at_1khz" (ldo) to the check strings — both are one extra analysis in the same ngspice `.control` block.

**11. Op-amp slew limit is modeled but never tested; phase margin silently unrepresentable.**
- WHAT: `opamp_ota` VA content includes "slew limit (tanh-limited output current)" but the TB is AC-only — AC linearizes, so slewing is invisible. Also `phase_margin_deg` is a spec field, but a 1-pole model has PM = 90 deg by construction.
- FIX: add "transient large-step check: output ramp rate == gbw-derived slew parameter within 20%" to the TB string, and add to the model-content string: "state in the header that phase margin is NOT represented (single-pole model, PM = 90 deg by construction)".

**12. Comparator RNM metastability dead-band should be required, not optional.**
- WHAT: The decision-class guidance makes the sensitivity dead-band "optional", but `min_input_sensitivity_mv` is a first-class spec field and the TB check references it — an implementer will reasonably skip the optional part and then the check ("flips at vth-offset within min_input_sensitivity_mv") tests nothing the model contains.
- FIX: change "optional sensitivity dead-band" to "required when min_input_sensitivity_mv is specified: for |vin - (vth - offset)| < min_input_sensitivity_mv/1000.0, hold the previous decision (deterministic v1 stand-in for the metastability window; note in the header that true metastability/random resolution is not modeled)".

**13. Small consistency/wording items (batch these):**
- LDO RNM check "settles to within 1% at the expected tau" — no spec field defines tau. Tell the builder to derive and *document* it (e.g. from the load cap and the rout judgment value) so the check isn't against an unstated number.
- Lock-time constant is 5/(2*pi*BW) in `_V_GUIDANCE["pll"]` but 4/(2*pi*BW) in `_RNM_GUIDANCE["phase_loop"]`. Harmless under the 2x tolerance, but unify on 4/(2*pi*BW) (first-order settling to ~2%) to avoid looking like an error.
- `$dist_normal` updates its seed argument by reference, so the *parameter* can't be passed directly: add "copy the seed parameter into an `integer` variable once at time 0 and pass that variable" to the jitter bullet in `RNM_RULES`.
- The ffe/dfe digital-Verilog denial reuses `_NO_DV_ANALOG` ("no bit-level behavior") — a DFE slices bits every UI. Give ffe/dfe their own reason string: "bit-only Verilog loses the real-valued tap weights/summing node that are the block's entire point; use RNM."
- RNM amp "response at the pole frequency" check: state the measurement method ("drive a sine at the pole frequency for >= 20 cycles, compare steady-state output/input amplitude ratio; do NOT attempt an FFT") so builders don't improvise.
- Honesty pass: a "compile_only" veriloga claim isn't cross-checked (e.g. that `<block>.osdi` exists). One-line addition to `check_models_in_summary` if cheap.

## Confirmed-correct items (so they aren't re-audited next cycle)
- Applicability matrix covers all 22 topology values, no block wrongly *allowed* a level; sampler/comparator VA denial is right (needs cross/timer events OpenVAF lacks); ffe/dfe VA denial right (absdelay unsupported).
- All parameter names in all three guidance tables exist in `TOPOLOGY_FIELDS` with matching units (dropout_mv aside — item 4 is a conversion problem, not a naming one).
- OpenVAF allowed/forbidden lists match the locally verified toolchain facts (incl. the `.model`-card-not-instance-params correction and `pre_osdi`-in-.control). No false permission found; no over-restriction blocking a legitimate construct found (noise functions are excluded as v1 scope, not claimed unsupported).
- Pass tokens have no substring collisions (V_TB_PASS is not a substring of VA_TB_PASS or RNM_TB_PASS).
- The discrete one-pole update, the Nyquist-response anti-aliasing check, opamp dominant pole = GBW/A0, $dist_normal's integer-fs convention, and the "model reflects the circuit actually built" rule are all correct as written.

## Recommended first real calibration run

**`nmos_current_mirror` with `models = [veriloga, rnm]`** — after fixing at least P1 item 2 (its own tanh formula).

Why this one:
- Cheapest transistor-level design in the catalog (2 devices, DC-only sims) AND the only topology with the hand-validated detailed prompt — the transistor half of the run is a known quantity, so any surprise is attributable to the new models section.
- It exercises two of the three model types, and critically the **only fully-verifiable chain on this machine today**: openvaf is installed and proven end-to-end, so the Verilog-A path can reach real "verified"; iverilog is still missing, so *any* topology's Verilog/RNM path stops at generated_unverified anyway — a comparator/PLL run (verilog+rnm) would verify nothing more while costing a transient-heavy transistor design. (If the user runs `sudo apt install iverilog` first, the same run also verifies the RNM path and triggers the deferred real-port probe — worth doing if possible.)

What to check in its output:
1. `nmos_current_mirror.va` compiles (`.osdi` exists) and uses no forbidden construct; compliance formula steepness — did it pass its DC check at V(out) = compliance without weakening the check (P1-2 regression test)?
2. TB is genuinely self-checking: the `.control` script computes Iout above/below compliance and branches to VA_TB_PASS/FAIL — not an unconditional echo. Confirm the below-compliance rolloff clause is present (a resistor to ground could otherwise pass a one-point check).
3. OSDI usage exactly per contract: `pre_osdi` inside `.control`, parameters on a `.model` card, `N`-prefixed instance line.
4. Model parameters reflect the *achieved* circuit: compliance defaulted to the measured Vds headroom and Iout to the measured mirrored current (including any systematic ratio error), not the ideal spec — this is the feature's core promise.
5. `summary.json.models`: veriloga "verified" backed by VA_TB_PASS in `va_sim.log`; rnm honestly "generated_unverified" with status_reason "iverilog not installed"; `checks` keyed by the same field names as target_spec; header comment block present in both model files.
6. The RNM `.sv`, though unrunnable, should still read correctly: `timescale 1ps/1fs`, real-valued iout per branch, seed-parameter jitter absent (no jitter field here — good), header stating what is not modeled.

---
Corrections from this audit (P1-1 idiom, P1-3 jitter method, tanh steepness numbers) have been merged into the researcher knowledge base at `~/.claude/agent-knowledge/circuit-topologies/behavioral-modeling.md`, including fixing the pole-idiom error that originated there.

## Implemented (cycle 3)
Implementer: Analog_Tool_Dev. All fixes in `backend/models_contract.py` unless noted; unit coverage in `backend/tests/test_models_contract.py` (50 checks), corrected-idiom proof in `backend/tests/test_va_fixture.py` + `backend/tests/fixtures/onepole.va`.

### Per-finding status
- **P1-1 pole idiom — FIXED.** `VA_RESTRICTIONS` now prescribes the current-contribution form `I(int) <+ gain*V(in) - V(int) - ddt(V(int))/wp` (plus the correct implicit voltage form), with an explicit do-NOT for the old voltage form. Proven end to end: fixture compiles under openvaf and measures DC gain 20.0000 dB / f3db 1.0000 MHz in ngspice; negative control with the old idiom measures 13.98 dB (exactly gain/2) and fails the TB. Cycle-2 spec section b1 corrected in place (strikethrough + pointer).
- **P1-2 mirror tanh — FIXED.** Both mirror guidance strings use `tanh(3*V(out)/Vcompliance)` with the tanh(3)=0.995 rationale spelled out, full-sweep flatness check above compliance, and the <80%-at-Vc/3 rolloff clause.
- **P1-3 PLL/CDR jitter — FIXED.** `RNM_RULES` + `phase_loop` guidance prescribe independent per-edge (synchronous) injection for locked loops; accumulation explicitly forbidden (free-running oscillators only, none in v1).
- **P2-4 LDO dropout units — FIXED** (`dropout_mv/1000.0` in both VA ldo and RNM reference guidance).
- **P2-5 RNM exponent units — FIXED** (`TS_s = TS_ps * 1e-12`, wp in rad/s, convert once into local reals).
- **P2-6 watchdog — FIXED** (mandatory watchdog bullet with per-level fail token in both `VERILOG_RULES` and `RNM_RULES`).
- **P2-7 exact-equality timing — FIXED** (0.1% or +/-2 fs tolerance in pll/comparator/sense_amp/DLL/decision checks + a general rule bullet).
- **P2-8 overdrive/saturation check — FIXED** (3x-overdrive clamp check added to ctle/vga/opamp_ota VA rows and the RNM amp class).
- **P3-9 output_driver VA — IMPLEMENTED** (moved to all-three-levels in the matrix with new VA guidance: tanh stage + series rout=output_impedance_ohm, DC-rout/swing TB checks; matrix comments updated; served to the form via /api/topologies).
- **P3-10 PSRR checks — IMPLEMENTED** (bandgap dVref/dVdd sweep, LDO 1 kHz AC ripple check).
- **P3-11 opamp slew + PM note — IMPLEMENTED** (transient large-step slew check; header must state PM=90 by construction).
- **P3-12 comparator dead-band — IMPLEMENTED** (REQUIRED when min_input_sensitivity_mv specified, deterministic hold + header note).
- **P3-13 batch — ALL IMPLEMENTED**: LDO tau must be derived and documented by the model; lock time unified on 4/(2*pi*BW); $dist_normal seed-copy-to-integer instruction; ffe/dfe get their own tap-weight denial string; RNM amp pole measurement method (sine, no FFT); honesty pass cross-checks `.osdi` on disk for veriloga "verified"/"compile_only" claims.

### Calibration run (89112a768b5b): 10 uA 1:2 nmos_current_mirror, veriloga+rnm, tt/27C/1.8V
Real pipeline, real API. success in 11.7 min, $6.06. Transistor result: Iout 22.117 uA vs 20 target (+10.6%, honest sky130 effective-width explanation), Vds 0.9 V. Checklist verdicts:
1. **PASS** — `.va` compiles (`.osdi` on disk), only allowed constructs (ddt/tanh/param ranges, finite rout+cout), `tanh(3V/Vc)` used; DC check passed at compliance *without weakening*: full-sweep 5% flatness above 0.9 V (nbad=0), Iout at Vc = 22.097 uA (-0.09%).
2. **PASS** — TB genuinely self-checking: `.control` computes a flatness mask over the whole sweep and branches to VA_TB_PASS/VA_TB_FAIL with reasons; rolloff clause present and measured (0.763 < 0.8 at Vc/3).
3. **PASS** — OSDI usage exact: `pre_osdi` in `.control`, `.model` card params, `NMIR` instance line.
4. **PASS** — model defaults are the ACHIEVED values (iout_ua=22.1166 incl. the systematic ratio error, vcompliance=0.9=measured Vds; RNM ratio=2.21166), with target-vs-achieved documented in headers. The feature's core promise held.
5. **PASS (one noted gap)** — veriloga "verified" backed by VA_TB_PASS in va_sim.log (survived the strengthened honesty pass incl. the .osdi check); rnm honestly "generated_unverified"/"iverilog not installed"; headers present. Gap: the hand-tuned mirror prompt has no `target_spec`/`measured` dicts, so model `checks` keys (iout_ua, vds_mirror_v, ...) can't line up with a `measured` object and the UI's "SPICE measured" column shows n/a for this topology only.
6. **PASS** — RNM `.sv` reads correctly: `timescale 1ps/1fs`, real variable-type ports, no jitter machinery (no jitter field — correct), static-block/tau-n.a. documented, TB has the watchdog and the same three tolerance-based checks.

### Tool bugs found and fixed
- **DISALLOWED_TOOLS blanket denies broke ALL Write/Edit** (`circuit_builder.py`): `Write(/**)`/`Write(~/**)` match every absolute path, including the run dir, and deny beats the relative `Write(**)` allow. Circuit_Builder worked around it by authoring every file through the allowed xschem Tcl interpreter (run still succeeded, but with wasted effort and writes outside the permission path). Replaced with targeted denies (backend/frontend/tools/libraries dirs, ~/.claude, ~/.volare); verified via a headless `claude -p` probe: in-cwd Write works, backend-dir Write blocked.
- `Bash(printenv *)` added to the allow list (was triggering approval stalls).
- `libraries.py` now files the compiled `.osdi` alongside the `.va` view so a filed cell is usable via `pre_osdi` without recompiling.

### Libraries-feature integration
Merged cleanly: backend starts with both changesets; both test scripts pass post-merge; `cal_runs` library created via POST /api/libraries and the calibration run filed into `cal_runs/current_mirror` (14 views incl. both model sets + .osdi); GET /api/libraries and per-view file serving verified; run record carries `library_filed` and the ResultsView banner shows it.

### Browser verification
15/15 headless checks on `?run=89112a768b5b` (models card, both rows, correct badges, checks table, source/log viewers incl. VA_TB_PASS in the log viewer, schematic PNG loads, library banner). Screenshot: `audits/2026-08-21-cycle3-calibration-results.png`.

### Remaining
- `sudo apt install iverilog` (user action) to unlock Verilog/RNM verification + the real-port probe; the mirror RNM TB is ready to run as-is.
- Mirror-topology checks/measured key alignment (item 5 gap) — small ResultsView or prompt tweak if wanted.
- output_driver's new Verilog-A path is contract-only so far (no real run has exercised it).
- Second allowed calibration run not used (first succeeded; no tool-reason failure).
