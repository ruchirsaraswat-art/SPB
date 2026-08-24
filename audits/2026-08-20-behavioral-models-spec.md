# Feature spec: behavioral model generation (Verilog-A / digital Verilog / RNM)

Date: 2026-08-20. Author: Circuit_Researcher (topology/modeling research). Audience:
analog-spec-tool developer (software engineer, no analog background assumed).
Scope: WHAT the tool should generate and HOW to verify it automatically. This spec
does not contain the models themselves — Circuit_Builder authors them per run, the
tool provides the contract (file names, parameter mapping, testbench pass tokens,
verification commands).

---

## a) Overview and recommended v1 scope

### What the three levels are for (one paragraph each, for the implementer)

- **Verilog-A** (`.va`): a continuous-time equation-level model that runs inside a
  SPICE simulator alongside transistor-level circuits. It captures transfer
  functions (gain, poles/zeros), saturation, and DC/temperature behavior with
  real voltages/currents. On this machine it is compiled by **OpenVAF** to a
  `.osdi` shared object that **ngspice** loads. Use case: swap one block of a
  multi-block SPICE testbench for a fast model.
- **Digital Verilog** (`.v`): an event-driven bit-level model (0/1 only, no
  voltages). It captures pin-accurate interfaces, clocked functional behavior
  (decisions, division, mux/demux), and delays as parameters. Use case: dropping
  the block into an RTL/digital simulation of the surrounding system.
- **RNM — real-number model** (`.sv`): event-driven like Verilog, but analog
  quantities travel as `real` values on ports. It is the industry workhorse for
  full-link/full-chip mixed-signal simulation (100-1000x faster than SPICE).
  Captures signal amplitudes, offsets, gains, settling/lock time, and jitter
  statistically — not waveform-accurate microvolts. Use case: whole-SerDes-lane
  system simulation with realistic amplitudes and timing.

### Applicability matrix (v1)

Not every level makes sense for every block. Generating a "digital Verilog CTLE"
is meaningless (a CTLE has no bits), and OpenVAF's Verilog-A subset cannot express
event-driven/clocked behavior (see section b1). Recommended v1 matrix — every
block gets **RNM plus at most one other level**:

| Block (topologies.py value) | Verilog-A | Digital Verilog | RNM |
|---|---|---|---|
| ctle, vga, tia, input_termination_buffer, opamp_ota | YES | no | YES |
| bandgap_reference, ldo, bias_current_mirror, nmos_current_mirror | YES | no | YES |
| comparator, sense_amp_slicer, sampler | no | YES | YES |
| pll, cdr, dll_phase_interpolator, clock_buffer_distribution | no | YES | YES |
| serializer, deserializer, output_driver | no | YES | YES |
| ffe, dfe | no | no | YES |
| custom | no (v1) | no (v1) | YES (best-effort) |

Rationale:
- **RNM everywhere**: it is the only level that can represent every block class
  (real values + events), and it is the level a SerDes system sim actually
  consumes. If only one level ships in v1, ship RNM.
- **Verilog-A only for continuous-time analog + references**: the locally
  available compiler (OpenVAF) supports the *compact-model* subset of Verilog-A —
  no `@(cross(...))` or timer events (only `initial_step`/`final_step`), no
  digital constructs, and the standard behavioral filters (`laplace_nd`,
  `transition`, `slew`, `absdelay`) are not documented as supported. Clocked and
  clock-generating blocks therefore cannot be written portably at this level on
  this toolchain. Continuous-time blocks and DC/temperature references fit the
  supported subset well (that subset exists precisely for temperature-dependent
  device equations).
- **Digital Verilog only for blocks with genuine bit-level behavior**: clocked
  decisions, clock generation/division, serialization. A digital model of an
  LDO or CTLE would be an empty stub; skip rather than generate noise.
- **FFE/DFE**: their essence is real-valued tap weights summed into a signal —
  bit-only Verilog loses the point, and Verilog-A needs `absdelay` (unsupported).
  RNM only.

### v1 rollout order (if phasing is needed)

1. RNM for: comparator, sampler, pll, ctle, serializer/deserializer (covers all
   four modeling patterns: clocked decision, phase-domain loop, sampled filter,
   bit datapath).
2. Verilog-A for: ctle, opamp_ota, bandgap_reference, nmos_current_mirror
   (covers the four Verilog-A patterns: pole/zero stage, gm-stage with slewing,
   temperature-dependent DC source, compliance-limited current source).
3. Everything else in the matrix.

---

## b) Per-model-type specification

### b1) Verilog-A (compiled with OpenVAF, simulated in ngspice via OSDI)

**Local toolchain facts (verified on this machine, 2026-08-20):**
- ngspice 45.2 at `/usr/bin/ngspice` has the OSDI loader built in. Its binary
  strings show it requires **OSDI API >= 0.4, i.e. an "OpenVAF-reloaded" build**
  of the compiler — the old original OpenVAF (osdi_0.3 tag, v23.5.0) will be
  *rejected* by this ngspice. Install the OpenVAF-reloaded binary (see section e).
- The `openvaf` binary is **NOT currently installed** — until it is, the tool
  must mark Verilog-A models "generated, not verified (openvaf missing)".
- Empirically probed: this ngspice build does **not** implement a `.osdi`
  dot-card (`unimplemented dot command '.osdi'`). The working load syntax is
  `pre_osdi <file.osdi>` inside the `.control` block; ngspice executes it
  *before* circuit parsing (verified with a dummy netlist), so `N`-prefixed
  instance lines in the same file resolve correctly.

**Verification commands the tool runs automatically (once openvaf installed):**
```
openvaf <block>.va                       # exit 0 and <block>.osdi exists = compile pass
ngspice -b <block>_va_tb.spice           # exit 0 AND log contains model checks (below)
```
Testbench skeleton the generated `<block>_va_tb.spice` must follow:
```
* <block> Verilog-A model testbench
.control
pre_osdi ./<block>.osdi
ac dec 20 1k 10G          $ or dc / temperature sweep as appropriate
* .meas-style checks via 'meas' or 'let' + 'print', then:
* echo VA_TB_PASS or VA_TB_FAIL <reason>
.endc
NX1 out in vdd vss <module_name> <param>=<value> ...
* stimulus sources...
.end
```
Pass criterion (machine-checkable): process exit code 0 AND the literal token
`VA_TB_PASS` in stdout/`sim.log` AND absence of `VA_TB_FAIL`. Circuit_Builder is
responsible for making the `.control` script compute the checks and echo exactly
one of those tokens.

**Mandatory coding restrictions (or OpenVAF will not compile it) — put these in
the Circuit_Builder prompt verbatim:**
- Allowed: `analog begin ... end` contribution statements (`V(out) <+ ...`,
  `I(out) <+ ...`), `ddt()`, `ddx()`, `limexp()`, `$temperature`, math functions
  (`tanh`, `ln`, `exp`, `pow`), parameters with ranges, internal nodes,
  `@(initial_step)`.
- Forbidden (unsupported by OpenVAF): `@(cross(...))`, `@(timer(...))`,
  `transition()`, `slew()`, `absdelay()`, `laplace_nd/laplace_zp/zi_*` filters,
  `$abstime`-driven event logic, digital (non-analog) blocks, bit shifts in
  analog blocks.
- Poles/zeros must be built as **internal-node state equations with `ddt()`**,
  not `laplace_*`. Pattern for one pole at `wp` (this is the standard idiom):
  ~~`V(int) <+ V(in)*gain - V(int) - ddt(V(int))/wp;  V(out) <+ V(int);`~~
  **[CORRECTED by cycle-3 audit P1-1 — the voltage form above is algebraically
  wrong (DC gain halved, pole doubled). Correct current-contribution form:
  `I(int) <+ gain*V(in) - V(int) - ddt(V(int))/wp;  V(out) <+ V(int);`
  or equivalently the implicit voltage form with NO `-V(int)` term:
  `V(int) <+ gain*V(in) - ddt(V(int))/wp;`. See
  audits/2026-08-21-cycle3-models-audit.md and backend/models_contract.py,
  which is the authoritative copy.]**
  (a zero is added by summing a scaled `ddt(V(in))` term into the equation).
- All limiting/clipping must be **smooth** (`tanh`-based), never `if (V(a)>x)`
  branch switches on signal voltages — discontinuous branches are the #1
  Verilog-A convergence trap in a Newton-Raphson simulator.
- Every model must present finite input/output impedance (e.g. `rin`, `rout`,
  `cout` parameters with defaults), never an ideal 0-ohm voltage contribution
  driving an arbitrary load — the other classic convergence/singular-matrix trap.
- Do NOT model in v1: device noise (`white_noise`/`flicker_noise` — support
  unverified locally and not needed for a functional model), mismatch/Monte
  Carlo, startup transients of references.

**Per block, what the model captures and which spec fields parameterize it**
(field names are the exact `topologies.py TOPOLOGY_FIELDS` keys; the tool passes
the user's entered values, Circuit_Builder fills judgment defaults for blanks and
must record them in `summary.json`):

| Block | Model content | Parameters from spec fields | TB check (drives VA_TB_PASS) |
|---|---|---|---|
| ctle | 1-zero/2-pole diff stage: DC gain, peak at Nyquist, output tanh clamp, rout+cload | dc_gain_db, peaking_db, data_rate_gbps (Nyquist=rate/2), cload_ff | AC sweep: gain(low-f) within 1 dB of dc_gain_db; gain(Nyquist)-gain(DC) within 1 dB of peaking_db |
| vga | gain set by a `gain_code` parameter: gain_db = gain_min_db + code*gain_step_db; single pole at bandwidth_ghz; tanh clamp at output_swing_vpp | gain_min_db, gain_max_db, gain_step_db, bandwidth_ghz, output_swing_vpp, cload_ff | AC at min and max code: gain within 1 dB of setting; -3 dB point within 20% of bandwidth_ghz |
| tia | I-in/V-out: transimpedance with single pole; input node capacitance; output clamp for max_input_current_ua | transimpedance_gain_kohm, bandwidth_ghz, input_capacitance_pf, max_input_current_ua | AC (current source in): Zt at DC within 10% of target; -3 dB within 20% |
| input_termination_buffer | input resistance = termination_impedance_ohm to a common-mode node; unity buffer with pole | termination_impedance_ohm, bandwidth_ghz, gain_db | DC input resistance within 5% of target; AC gain ~gain_db |
| opamp_ota | classic 1-pole gm/rout integrator: dc gain, GBW, slew limit (tanh-limited output current), swing clamp; dominant pole = GBW/(10^(dc_gain_db/20)) | dc_gain_db, gbw_mhz, cload_pf, output_swing_vpp | AC open-loop: DC gain within 2 dB; unity-gain freq within 20% of gbw_mhz |
| bandgap_reference | V(out) <+ vref*(1 + tc*(T-27)*1e-6) + vdd sensitivity term (psrr_db); uses `$temperature` | output_voltage_v, temp_coefficient_ppm_per_c, psrr_db | DC temp sweep (range from temp_range_c): mean Vref within 2% of target; measured tempco within 2x of spec value |
| ldo | vout = smooth-min(output_voltage_v, V(vdd) - dropout_mv); finite rout so load current causes small droop; supply ripple attenuation per psrr_db_at_1khz | output_voltage_v, max_load_current_ma, dropout_mv, psrr_db_at_1khz | DC sweep of Vin: regulation region flat within 1%; dropout onset within 20% of spec |
| bias_current_mirror / nmos_current_mirror | I(out) <+ Iout_target * tanh(V(out)/compliance) — current source with smooth compliance rolloff, one branch per output | output_current_ua (or iref_ua*ratio), compliance_voltage_v, num_output_branches | DC: Iout within matching % above compliance voltage; drops off below it |

### b2) Digital Verilog (compiled/run with Icarus Verilog)

**Local toolchain facts:** `iverilog`/`vvp` are **NOT installed** (verified
2026-08-20). Until installed (small Debian package — section e), digital Verilog
and RNM models are "generated, not verified". `verilator` is also absent;
optional later as a lint-only second opinion (`verilator --lint-only`).

**Verification commands:**
```
iverilog -g2012 -o <block>_v_tb.vvp <block>.v <block>_v_tb.v   # exit 0 = compile pass
vvp <block>_v_tb.vvp                                            # exit 0 AND prints V_TB_PASS
```
Pass criterion: exit 0 AND literal `V_TB_PASS` AND no `V_TB_FAIL` in stdout. The
testbench must be self-checking (`if (expected !== got) begin $display("V_TB_FAIL ..."); $finish; end`),
end with `$display("V_TB_PASS"); $finish;`, and use
`` `timescale 1ps/1fs `` (GHz-class periods need sub-ps precision; with 1ns/1ps
timescale a 2 GHz half-period of 250 ps quantizes badly and duty/jitter params
silently truncate to zero — a standard event-driven-modeling trap).

**What this level captures / must NOT capture:**
- Captures: pin-accurate ports (data, clocks, resets, enable, digital control
  words like VGA gain code or PI phase code), clocked function, delays as
  `parameter real`-free plain parameters in ps (use `#(DELAY_PS)` with the ps
  timescale).
- Must NOT attempt: voltages, offsets in mV, metastability randomness, jitter
  (an event-driven scheduler has no analog noise; faking jitter here creates
  false confidence), loop dynamics (a Verilog PLL locks "instantly" — that is
  fine and should be stated in a header comment).

**Per block:**

| Block | Model content | Parameters from spec fields | TB check |
|---|---|---|---|
| comparator / sense_amp_slicer | clocked D-decision: `q <= d` on clk edge with clk-to-q delay param; ports for calibration code retained but functionally inert (documented) | propagation_delay_ps / regeneration_time_ps as CLK2Q_PS | drive pattern, check q sequence + measured delay == parameter |
| sampler | flop with aperture delay param | sampling_rate_gsps (TB clock), propagation delay | pattern retiming check |
| pll | output clock generator: `always #(HALF_PERIOD_PS)` from output_freq_ghz; divider to fb clock; `lock` flag asserted LOCK_CYCLES after enable | ref_freq_mhz, output_freq_ghz (N = ratio must be integer-checked), loop_bandwidth_mhz -> LOCK_TIME approx 5/(2*pi*BW) mapped to cycles | measure output period over 100 cycles == 1/output_freq_ghz; lock flag timing |
| cdr | recovered clock generator at data_rate_gbps + data retimer flop | data_rate_gbps, loop_bandwidth_mhz (lock-time param) | retimed data matches delayed input pattern |
| dll_phase_interpolator | phase-select delay line: out = clk delayed by code*phase_step_ps | clock_freq_ghz, num_phases, phase_step_ps | sweep code, measure delay == code*step |
| clock_buffer_distribution | fanout_loads output copies, each delayed BUF_DELAY_PS; duty preserved | clock_freq_ghz, fanout_loads, duty_cycle_pct | all outputs toggle, measured period/duty correct |
| serializer | width-bit shift register, load on word clock, shift at serial rate; serial clock derived or input | parallel_width_bits, serial_data_rate_gbps, parallel_clock_freq_mhz | known word in, serial pattern out, bit order asserted |
| deserializer | mirror of serializer + word-alignment counter | serial_data_rate_gbps, parallel_width_bits | loopback with serializer model or fixed pattern |
| output_driver | non-inverting buffer with rise/fall as separate min:typ:max delays | data_rate_gbps, rise_fall_time_ps | edge-to-edge delay check |

### b3) RNM — real-number models (SystemVerilog with real-valued ports)

**Format decision (this is the important one):** industry RNM styles are
SystemVerilog **`nettype real`** (user-defined nettypes, IEEE 1800-2012) or
Verilog-AMS **`wreal`**. *Neither is supported by Icarus Verilog.* Therefore v1
generates **SystemVerilog modules whose ports are variable type `real`**
(`input real`, `output real`) — legal SV, the same abstraction, and the closest
subset to what commercial simulators (VCS/Xcelium/Questa) accept; a one-line
`nettype` wrapper can be added later for commercial flows without touching the
core model. File extension `.sv`, compiled with `iverilog -g2012`.

- **Risk flag (be honest with the implementer):** Icarus's SystemVerilog support
  is a partial subset and real-typed *ports* specifically have had patchy support
  historically. On first setup, run this 10-line probe once:
  ```
  // probe_realport.sv
  module src(output real r); initial r = 1.25; endmodule
  module top; real x; src u(.r(x)); initial #1 if (x == 1.25) $display("REALPORT_OK"); endmodule
  ```
  `iverilog -g2012 -o p probe_realport.sv && vvp p` -> if `REALPORT_OK`, use
  real ports everywhere. If it fails, fall back to the fully portable
  plain-Verilog encoding: ports as `wire [63:0]` carrying IEEE-754 bits via
  `$realtobits`/`$bitstoreal` at each boundary (works in every Verilog-2005
  simulator; ugly but mechanical). The tool should store which mode was probed
  and put it in the Circuit_Builder prompt.

**Verification commands:**
```
iverilog -g2012 -o <block>_rnm_tb.vvp <block>_rnm.sv <block>_rnm_tb.sv
vvp <block>_rnm_tb.vvp        # exit 0 AND prints RNM_TB_PASS, no RNM_TB_FAIL
```

**Modeling rules to hand Circuit_Builder (RNM-specific pitfalls):**
- `` `timescale 1ps/1fs `` in every file. Jitter/phase-step parameters in ps can
  be fractional; with coarser precision they quantize to zero silently.
- Event-driven models only evaluate on events. Continuous dynamics (a CTLE pole,
  LDO settling) need a **model sample clock**: a free-running internal `always
  #TS` tick with TS <= 1/(20 x highest pole or 0.25 UI, whichever smaller), and
  the discrete one-pole update `y <= y + (1.0-$exp(-TS*w_pole))*(x-y)`.
  Undersampling this tick aliases the frequency response — that is THE classic
  RNM bug; the TB must check response at Nyquist, which catches it.
- Directional signal flow only: an RNM output *tells* the next block a value; it
  does not present an impedance. Do not attempt bidirectional termination /
  reflection modeling (out of scope at this level; note it in the model header).
- Jitter: model as Gaussian edge-time perturbation using `$dist_normal(seed,0,sigma_fs)`
  divided down to ps — the Kundert jitter-modeling approach (accumulating jitter
  for oscillators: add the sample per period; synchronous jitter for
  buffers/dividers: independent per edge). Seed must be a module parameter so
  runs are reproducible.
- PLL/CDR are **phase-domain** models: keep real state variables `freq_hz` and
  `phase`, step them toward lock with a first-order (or second-order) discrete
  loop whose time constant comes from loop_bandwidth_mhz; generate output edges
  from the phase accumulator. Do not model the VCO waveform.
- What NOT to model: sub-sample waveform shape, absolute accuracy below ~1% (use
  Verilog-A/SPICE for that), supply nets as analog (a plain `real vdd_v`
  parameter is enough in v1).

**Per block class (parameters = same topologies.py keys as b1/b2 tables):**

| Class | RNM content | Key TB checks |
|---|---|---|
| ctle/vga/tia/opamp/termination | sampled-data gain + discrete 1-2 pole filter + saturation; VGA gain code port; TIA input is a real current | step + two-tone-free checks: DC gain within 1 dB (TIA: Zt within 10%); response at pole freq -3 dB +/- 1 dB (catches tick aliasing) |
| comparator/sense_amp/sampler | on clk edge: `q <= (vin + offset_mv/1000.0) > vth`; optional sensitivity dead-band -> retain previous value (deterministic, not random, in v1); clk-to-q delay | sweep vin across threshold: decision flips at vth-offset within min_input_sensitivity_mv; delay == parameter |
| pll/cdr/dll_pi | phase-domain loop (above): lock flag when freq error < 0.1%; lock time approx 4/(2*pi*loop_BW); per-edge jitter injection from rms_jitter_ps | measured mean period == target within 0.1%; lock time within 2x of 4/(2*pi*BW); measured edge-time stddev within 20% of rms_jitter_ps (>=1000 edges) |
| bandgap/ldo/mirror | real outputs: vref(temp) polynomial from tempco; LDO: vout = min(vset, vin-dropout) + 1st-order load-step settling + droop = Iload*rout; mirror: real iout per branch | temp argument sweep -> tempco within 2x; load step -> settles to within 1% at expected tau; dropout onset |
| ffe/dfe | FFE: UI-spaced real delay line, out = sum(tap_i * x[n-i]), taps as real parameter array; DFE: comparator RNM + real feedback sum of last num_taps decisions * weights | FFE: impulse-in -> tap weights read back exactly; DFE: canned ISI pattern corrected (known-answer test) |
| serializer/deserializer | bits + real timing (UI from serial rate); same function as b2 but edges placed on real time grid | loopback known-answer |
| output_driver | bit in -> real out swinging +/- output_swing_vpp/2 with linear-ramp edges of rise_fall_time_ps (piecewise updates on the sample tick) | swing and 20-80% time checks |

---

## c) What the Circuit_Builder prompt must be told to produce

Extend `build_prompt()` (both the current-mirror and generic branches) with a
conditional "Behavioral models" section when any model checkbox is set. Contract:

**Deliverable files, per selected model type, in the run directory** (fixed names
so `main.py` artifact-existence checking keeps working):
- Verilog-A: `<block>.va`, `<block>.osdi` (if openvaf available), `<block>_va_tb.spice`, `va_sim.log`
- Verilog: `<block>.v`, `<block>_v_tb.v`, `v_sim.log`
- RNM: `<block>_rnm.sv`, `<block>_rnm_tb.sv`, `rnm_sim.log`
where `<block>` is the topology value string (e.g. `ctle`).

**Prompt content to add (summarized; lift restriction lists verbatim from b1/b3):**
1. Which model types to produce this run (from the UI checkboxes), and for each
   the applicable row of the b-tables: what to capture, which spec fields become
   which parameters, what the TB must check.
2. The exact verification command(s) to run and the pass-token protocol
   (`VA_TB_PASS` / `V_TB_PASS` / `RNM_TB_PASS`, fail variants). Circuit_Builder
   runs them itself and reports honestly — same philosophy as the existing
   "report exactly what ngspice measured" rule.
3. The OpenVAF forbidden-construct list (b1) and RNM rules (b3) verbatim — these
   are hard compile-compatibility constraints, not style advice.
4. Every model parameter must default to the *target spec value*; where the user
   left a field blank, use the same judgment value as the transistor-level design
   and keep the two consistent (the model must describe the circuit actually
   built, not the ideal spec — e.g. if the schematic only achieved 8 dB peaking
   vs a 10 dB target, set the model parameter to 8 dB and note it).
5. A header comment block in every model file: block, date, abstraction level,
   parameter/spec mapping table, what is NOT modeled, verification status.
6. `summary.json` gains one new top-level key (only when models were requested):
   ```
   "models": {
     "veriloga": {"file": "ctle.va", "testbench": "ctle_va_tb.spice",
                   "sim_log": "va_sim.log",
                   "status": "verified" | "compile_only" | "generated_unverified" | "failed" | "not_applicable",
                   "status_reason": "<e.g. 'openvaf not installed'>",
                   "checks": {"peaking_db": {"target": 10, "model_measured": 9.8}, ...}},
     "verilog":  { ...same shape... },
     "rnm":      { ...same shape... }
   }
   ```
   `main.py`'s summary post-check should verify claimed model files exist on disk
   (extend the existing artifact-missing logic) and downgrade "verified" to
   "failed" if the pass token is absent from the named sim log.
7. Status semantics: `verified` = compile AND self-checking TB run passed;
   `compile_only` = compiled, TB not runnable; `generated_unverified` = required
   tool missing on this machine (say which); `failed` = compile or TB failed
   (include the error); `not_applicable` = matrix says skip (tool should not
   even request these).

---

## d) UI implications

- **Spec form:** a "Behavioral models" group with three checkboxes: Verilog-A,
  Digital Verilog, RNM (SystemVerilog real). Default state and enablement come
  from the applicability matrix (serve it from `topologies.py` alongside
  `fields` so the frontend stays data-driven, e.g. per-option key
  `"models": {"veriloga": true, "verilog": false, "rnm": true}`). Disabled
  checkboxes get a tooltip saying *why* (e.g. "Verilog-A skipped: clocked blocks
  can't be expressed in the OpenVAF-supported subset on this toolchain").
- If a required tool is missing (openvaf / iverilog), keep the checkbox enabled
  but show an inline warning: "will be generated but only <compile-checked |
  unverified> — <tool> not installed" (backend exposes tool availability via a
  small `GET /api/toolchain` probe: `shutil.which` for openvaf/iverilog/vvp).
- **Results panel:** a "Behavioral models" card listing one row per requested
  model type: file name (download link), status badge (verified / compile-only /
  unverified / failed with reason), and the model-vs-target checks table from
  `summary.json.models.*.checks`. Where the transistor-level run measured the
  same metric, show a third column so the user sees spec target vs SPICE
  measured vs model measured — model/SPICE disagreement is exactly what this
  feature exists to surface.
- Run directory listing already shows files; no change needed there.

---

## e) Prerequisites / missing tools (verified on this machine 2026-08-20)

| Tool | Status | Needed for | Action |
|---|---|---|---|
| ngspice 45.2 (OSDI loader built in, requires OSDI >= 0.4) | INSTALLED, `/usr/bin/ngspice` | running Verilog-A TBs | none |
| openvaf (must be **OpenVAF-reloaded**, emits OSDI 0.4 — the old pre-rename osdi_0.3 binary is rejected by ngspice 45.2) | **MISSING** | compiling `.va` -> `.osdi` | download Linux binary from https://openvaf.semimod.de/download/ or OpenVAF-Reloaded GitHub releases; single static binary, put on PATH |
| iverilog + vvp (Icarus Verilog) | **MISSING** | compiling+running Verilog and RNM TBs | `sudo apt install iverilog` (small package). Then run the real-port probe from b3 once and record the result |
| verilator | MISSING (optional) | extra lint pass only | skip for v1 |
| Xyce / commercial AMS simulator | absent | true `nettype real`/`wreal`/full Verilog-AMS | out of scope; noted so nobody expects `@(cross)`-style Verilog-A to work here |

Until the two missing tools are installed, the feature still works end-to-end but
every model lands in `generated_unverified` status — acceptable for development,
not for trusting the models.

---

## Sources

- OpenVAF Verilog-A standard compliance (events limited to initial/final_step,
  digital constructs unparsed): https://openvaf.semimod.de/docs/details/verilog-a-standard/
  and https://man.sr.ht/~dspom/openvaf_doc/language_support.md
- ngspice OSDI/OpenVAF workflow: https://ngspice.sourceforge.io/osdi.html (ngspice
  manual ch. 13); `pre_osdi`-in-.control behavior verified locally by probe netlist
- OSDI 0.3 vs 0.4 / OpenVAF-reloaded rename: https://github.com/arpadbuermen/OpenVAF
  and https://github.com/OpenVAF/OpenVAF-Reloaded ; requirement "OSDI >= 0.4"
  confirmed from local /usr/bin/ngspice binary strings
- Example Verilog-A models known to compile under OpenVAF (style reference for
  Circuit_Builder): https://github.com/dwarning/VA-Models
- Icarus Verilog SV subset (partial; extensions doc claims logic/bit/real port
  intent): https://steveicarus.github.io/iverilog/usage/icarus_verilog_extensions.html ,
  https://github.com/steveicarus/iverilog — real-port support treated as
  "probe at install time", not assumed
- PLL phase-domain + jitter modeling patterns (accumulating vs synchronous
  jitter, phase-domain loop): K. Kundert, "Modeling Jitter in PLL-based
  Frequency Synthesizers" and "Modeling and Simulation of Jitter in PLLs",
  https://designers-guide.org/analysis/PLLjitter.pdf , https://kenkundert.com/docs/aacd97.pdf
- Spec-field-to-parameter mapping: backend/topologies.py TOPOLOGY_FIELDS (this
  repo) and ~/.claude/agent-knowledge/circuit-topologies/serdes-phy-spec-fields.md

---

## Implemented (cycle 2)

Date: 2026-08-20 (Analog_Tool_Dev). Everything in sections a-d is implemented
except the explicitly deferred items below.

### What was built
- **`backend/models_contract.py` (new)** - single home for the model feature's
  domain knowledge: the applicability matrix (`MODEL_APPLICABILITY`, exactly the
  section-a table incl. `nmos_current_mirror` and best-effort-RNM `custom`),
  request validation (422 for a model type the matrix rejects), runtime
  toolchain probing (`toolchain_status()`: openvaf/iverilog/vvp/ngspice via
  `shutil.which` over a PATH that includes the project-local `tools/bin`, plus
  the one-shot Icarus real-port probe, disk-cached in `tools/realport_probe.json`),
  the prompt-contract builder (`build_models_prompt_section()`: fixed deliverable
  filenames, b1/b3 restriction lists verbatim, per-topology b-table guidance,
  pass-token protocol, summary.json `models` schema + status semantics, and
  conditional "tool X missing -> generated_unverified" notes), and the post-run
  honesty pass (`check_models_in_summary()`: claimed files must exist on disk;
  a "verified" claim without its pass token - or with a fail token - in the
  named sim log is downgraded to "failed"; missing requested types are
  synthesized as "failed" entries). Downgrades are per-model and do not fail the
  transistor-level run.
- **Backend wiring**: `RunSpec.models` (list of veriloga/verilog/rnm, validated);
  `GET /api/toolchain`; per-option `"models"` map (+ top-level `custom_models`)
  on `GET /api/topologies`; `build_prompt()` appends the models section in BOTH
  the current-mirror and generic branches; Circuit_Builder's allow-list gains
  `Bash(openvaf *)`, `Bash(iverilog *)`, `Bash(vvp *)` and its subprocess PATH
  is prefixed with `tools/bin`; `run_circuit_builder()` runs the honesty pass
  and appends any model problems to summary notes.
- **Frontend**: spec-form "Behavioral models" fieldset (three checkboxes,
  enablement + disabled-reason tooltips/inline text from the served matrix;
  amber "will be generated but NOT verified - <tool> missing" warning driven by
  `/api/toolchain`; checkboxes cleared on topology change; opt-in default -
  nothing pre-checked, since model generation lengthens runs);
  ResultsView "Behavioral models" card (per-type status badge, target vs
  SPICE-measured vs model-measured checks table, download links, inline viewers
  for model source / testbench / verification log).
- **Cycle-1.5 leftovers folded in**: ResearchView now shows the persisted
  submit-time warnings box; `GET /api/runs` list entries include `warnings`;
  stale `backend/main.py.patch` confirmed already-applied (its hunks are all
  present, corner list is now a superset) and deleted.

### Toolchain outcome on this machine
- **openvaf: INSTALLED, user-space, verified end-to-end.** The semimod.de
  download page only carries old pre-reloaded builds (osdi 0.3, rejected by
  ngspice 45.2), so the OpenVAF-Reloaded GitHub release v24.0.1mob
  (linux-x86_64) was installed under `tools/openvaf-r-v24.0.1mob-linux-x86_64/`
  with a `tools/bin/openvaf` symlink (it is not a single static binary - it
  ships a private libLLVM). `openvaf --version` -> "OpenVAF-reloaded unknown".
  Full chain proven: a compact-model-subset `.va` compiles to `.osdi`, ngspice
  45.2 loads it via `pre_osdi` in `.control`, and a self-checking TB printed
  `VA_TB_PASS`. **Contract correction discovered while probing:** OSDI instance
  parameters on the `N` instance line (as sketched in b1) do NOT parse - the
  working syntax is a `.model <mname> <module> <param>=<value>` card plus
  `N<inst> <nodes> <mname>`. This is baked into the prompt contract.
- **iverilog/vvp: NOT INSTALLED - user prerequisite.** `sudo -n` requires a
  password on this machine, so no auto-install was attempted. Until the user
  runs `sudo apt install iverilog`, digital-Verilog and RNM models land as
  `generated_unverified` (UI warns inline; prompt tells Circuit_Builder to skip
  verification honestly), and the b3 real-port probe is deferred (`realport_probe:
  null`; it runs automatically on the first toolchain probe after install, and
  the prompt contract switches to the `$realtobits` fallback if it fails).

### Verification evidence (no paid Circuit_Builder runs used)
- 43 backend unit checks: matrix/validation, prompt-builder content for both
  branches (deliverable names, restriction lists, pass tokens, conditional
  tool notes, no-models = unchanged prompt), honesty-pass downgrade paths, and
  RunSpec 422s.
- Fabricated fixture run (ctle, veriloga+rnm) with a real openvaf-compiled
  `.va` + ngspice-run TB (`VA_TB_PASS` in `va_sim.log` - the spec's b1
  verification commands executed for real) and an unverifiable RNM pair; the
  honesty pass accepted "verified" for VA and left RNM at
  generated_unverified; live-API checks confirmed run detail, file serving,
  list `warnings`, `/api/toolchain`, and the pll+veriloga 422.
- Headless-browser pass: 19/19 checks on the form checkboxes (per-topology
  enablement incl. custom, disabled reasons, iverilog warning appearing only
  when relevant) and the results models card (badges, 3-column checks table,
  inline source/log viewers); plus 2/2 on the ResearchView warnings box via a
  stubbed-researcher submit. Screenshots:
  `2026-08-20-cycle2-form-model-checkboxes.png`,
  `2026-08-20-cycle2-results-models-card.png`,
  `2026-08-20-cycle2-researchview-warnings.png`. oxlint clean for touched
  files. Fixture/stub runs deleted afterwards; real backend restored.

### Deferred
- Icarus real-port probe execution + fallback-mode selection: automatic once
  iverilog is installed (code path exists and is prompt-wired; untestable until
  then).
- Verilator lint second opinion (spec marks it optional/skip for v1).
- First real Circuit_Builder run with models enabled: intentionally not spent
  in this cycle; the prompt contract is unit-verified, but expect one
  calibration pass on a cheap block (e.g. bias_current_mirror, veriloga+rnm)
  the first time it runs for real.
- Cycle-1.5 minor leftover NOT addressed (unchanged): mixed field+cross-field
  422s still report only the field error.

### Addendum (same session, user request mid-cycle)
Start-screen behavior changed per user message: the form no longer opens
pre-loaded with the NMOS current-mirror spec. On load the topology select
shows a disabled "Select a topology..." placeholder and every spec field
(block targets, budgets, corner/temp, testbench style, behavioral-model
checkboxes, submit button) stays hidden, with a hint pointing at the
architecture diagram. Clicking a diagram block (or picking from the dropdown)
reveals that topology's spec section; the current-mirror sizing defaults
still apply once it is explicitly chosen. Verified headlessly (13/13 checks;
screenshots `2026-08-20-cycle2-empty-start-screen.png`,
`2026-08-20-cycle2-after-block-click.png`). File: frontend/src/SpecForm.jsx.

### Addendum 2 (same session, user request): hierarchical design libraries (lib/cell/view)
How xschem actually handles "database" structure (checked on this machine):
there is no design database - XSCHEM_LIBRARY_PATH in xschemrc is a list of
directory roots, each directory on it is a "library" in the browser, and
cells are plain .sch/.sym files referenced by root-relative path (the sky130
PDK's xschem library is exactly that). The lib -> cell -> view
hierarchy is therefore imposed as a directory convention xschem can browse:
`libraries/<library>/<cell>/<view files>` (schematic/symbol/netlist/
testbench/image/behavioral-model/measurement views + provenance.json naming
the producing run). The libraries/ root is appended to every run's
XSCHEM_LIBRARY_PATH, so filed cells' symbols are instantiable from any run's
xschem session as `<library>/<cell>/<cell>.sym`.
- backend/libraries.py (new): name validation, create/list, and
  file_run_into_library() which copies a successful run's views out of the
  immutable runs/<id>/ dir (latest filing wins; run history keeps every
  version). main.py: GET/POST /api/libraries, per-view file serving,
  optional validated RunSpec.library, filing on success with
  `library_filed`/`library_error` recorded on the run (a filing failure
  never fails the build itself).
- Frontend: the form now asks for a design library once a topology/block is
  chosen - dropdown of existing libraries plus inline "+ Create new
  library..." (name validated server-side, readable 422s); submit stays
  disabled until a library is chosen. ResultsView shows "Filed into design
  library: <lib> / <cell> (n views)" or the filing error.
- Verified: 15 backend unit checks (names, idempotent create, view copying
  incl. symbols/models, RunSpec validation, xschemrc content), live API
  checks, 11/11 headless-browser checks; screenshots
  `2026-08-20-cycle2-library-picker.png`,
  `2026-08-20-cycle2-results-library-filed.png`. Deferred: a dedicated
  library-browser panel in the UI (cells/views are listed by GET
  /api/libraries and browsable in xschem itself; build a UI panel when the
  user asks for one).
