"""
Behavioral-model generation contract (cycle 2, user request).

Everything the tool knows about the three behavioral-model abstraction
levels lives here, driven by the Circuit_Researcher feature spec at
audits/2026-08-20-behavioral-models-spec.md:

  - which model types make sense for which topology (MODEL_APPLICABILITY -
    every block gets RNM plus at most one other level, except output_driver
    which gets all three - see the matrix comment);
  - the local verification toolchain (openvaf -> OSDI -> ngspice for
    Verilog-A; iverilog/vvp for digital Verilog and RNM) and runtime
    probing of what is actually installed (toolchain_status());
  - the per-type prompt contract handed to Circuit_Builder
    (build_models_prompt_section()): fixed deliverable filenames, verbatim
    OpenVAF/RNM coding restrictions, self-checking-testbench pass tokens,
    and the summary.json "models" schema;
  - post-run honesty checking (check_models_in_summary()): claimed model
    files must exist on disk and a "verified" claim must be backed by the
    pass token in the named sim log, else the per-model status is
    downgraded to "failed".

Local toolchain facts baked in (verified on this machine, 2026-08-20):
  - ngspice 45.2 has the OSDI loader built in but requires OSDI API >= 0.4,
    i.e. an OpenVAF-RELOADED build; the old openvaf 23.5.0 is rejected.
    The working OSDI install here is the OpenVAF-reloaded static build under
    tools/openvaf-r-*/, symlinked as tools/bin/openvaf.
  - ngspice does NOT implement a `.osdi` dot-card; the working load syntax
    is `pre_osdi <file.osdi>` inside the .control block.
  - OSDI instance syntax (probed empirically - the feature spec's sketch of
    params on the instance line does NOT parse): a `.model <mname> <module>
    <param>=<value> ...` card plus an instance line `N<inst> <nodes> <mname>`.
  - iverilog/vvp are not installed and cannot be installed without sudo
    (interactive password required) - recorded as a user prerequisite;
    until installed, Verilog/RNM models land as generated_unverified and
    the SystemVerilog real-port probe (REALPORT_OK) cannot be run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
# Project-local tool installs (no sudo on this machine). Prepended to PATH
# for both runtime probes here and the Circuit_Builder subprocess in
# circuit_builder.py, so `openvaf` resolves without touching the user's
# shell profile.
TOOLS_BIN = BASE_DIR / "tools" / "bin"

MODEL_TYPES = ("veriloga", "verilog", "rnm")
MODEL_LABELS = {
    "veriloga": "Verilog-A (OpenVAF/OSDI, runs inside ngspice)",
    "verilog": "Digital Verilog (event-driven, bit-level)",
    "rnm": "RNM - real-number model (SystemVerilog, real-valued ports)",
}
PASS_TOKENS = {"veriloga": "VA_TB_PASS", "verilog": "V_TB_PASS", "rnm": "RNM_TB_PASS"}
FAIL_TOKENS = {"veriloga": "VA_TB_FAIL", "verilog": "V_TB_FAIL", "rnm": "RNM_TB_FAIL"}

# Why a disabled checkbox is disabled - shown as a tooltip in the form, so
# "no digital Verilog for a CTLE" reads as a reasoned decision, not a bug.
_NO_VA_CLOCKED = (
    "Verilog-A skipped: clocked/event-driven behavior can't be expressed in the "
    "OpenVAF-supported compact-model subset on this toolchain (no @(cross), no "
    "timer events, no transition())."
)
_NO_VA_DELAY = (
    "Verilog-A skipped: FFE/DFE need absdelay()/laplace-style delay elements, "
    "which the local OpenVAF compiler does not support. Use RNM."
)
_NO_VA_CUSTOM = "Verilog-A skipped for custom blocks in v1 (no per-block model contract exists)."
_NO_DV_ANALOG = (
    "Digital Verilog skipped: this block has no bit-level behavior - a 0/1-only "
    "model of it would be an empty stub."
)
_NO_DV_EQUALIZER = (
    "Digital Verilog skipped: bit-only Verilog loses the real-valued tap "
    "weights/summing node that are the block's entire point; use RNM."
)
_NO_DV_CUSTOM = "Digital Verilog skipped for custom blocks in v1 (no per-block model contract exists)."

_CONTINUOUS_ANALOG = (
    "ctle", "vga", "tia", "input_termination_buffer", "opamp_ota",
    "bandgap_reference", "ldo", "bias_current_mirror", "nmos_current_mirror",
)
_CLOCKED_BIT = (
    "comparator", "sense_amp_slicer", "sampler",
    "pll", "cdr", "dll_phase_interpolator", "clock_buffer_distribution",
    "serializer", "deserializer",
)

# Applicability matrix (feature spec section a): every block gets RNM plus at
# most one other level - EXCEPT output_driver, which gets all three (cycle-3
# audit P3-9: its limiting stage is not clocked, `tanh` behind a series rout
# is squarely inside the OpenVAF subset, and Verilog-A is the ONLY level that
# can represent output_impedance_ohm, the driver's second headline spec -
# RNM/Verilog are directional and cannot present an impedance). Values:
# True = offered, or a string = not offered, with the string being the
# user-facing reason.
MODEL_APPLICABILITY: dict[str, dict[str, Any]] = {}
for _t in _CONTINUOUS_ANALOG:
    MODEL_APPLICABILITY[_t] = {"veriloga": True, "verilog": _NO_DV_ANALOG, "rnm": True}
for _t in _CLOCKED_BIT:
    MODEL_APPLICABILITY[_t] = {"veriloga": _NO_VA_CLOCKED, "verilog": True, "rnm": True}
MODEL_APPLICABILITY["output_driver"] = {"veriloga": True, "verilog": True, "rnm": True}
for _t in ("ffe", "dfe"):
    MODEL_APPLICABILITY[_t] = {"veriloga": _NO_VA_DELAY, "verilog": _NO_DV_EQUALIZER, "rnm": True}
MODEL_APPLICABILITY["custom"] = {"veriloga": _NO_VA_CUSTOM, "verilog": _NO_DV_CUSTOM, "rnm": True}


def supported_model_types(topology: str) -> list[str]:
    matrix = MODEL_APPLICABILITY.get(topology) or MODEL_APPLICABILITY["custom"]
    return [t for t in MODEL_TYPES if matrix.get(t) is True]


def validate_models_request(topology: str, models: list[str] | None) -> None:
    """Raises ValueError (surfaced as 422 by main.py's model validator) for
    unknown model-type strings or a type the applicability matrix says makes
    no sense for this topology (e.g. digital Verilog for a CTLE)."""
    if not models:
        return
    unknown = sorted(set(models) - set(MODEL_TYPES))
    if unknown:
        raise ValueError(
            f"unknown behavioral model type(s): {', '.join(unknown)} - "
            f"must be among {list(MODEL_TYPES)}"
        )
    matrix = MODEL_APPLICABILITY.get(topology) or MODEL_APPLICABILITY["custom"]
    for m in models:
        if matrix.get(m) is not True:
            raise ValueError(
                f"behavioral model type '{m}' is not applicable to topology "
                f"'{topology}': {matrix.get(m)}"
            )


# ---------------------------------------------------------------------------
# Runtime toolchain probing
# ---------------------------------------------------------------------------

def augmented_path(base_path: str) -> str:
    """PATH with the project-local tools/bin prepended."""
    return f"{TOOLS_BIN}:{base_path}" if TOOLS_BIN.is_dir() else base_path


def _which(name: str) -> str | None:
    import os
    return shutil.which(name, path=augmented_path(os.environ.get("PATH", "")))

_toolchain_cache: dict[str, Any] | None = None


def _probe_realport(iverilog: str, vvp: str) -> str:
    """One-shot probe (feature spec b3): does this Icarus build support
    real-typed module ports? Result is cached on disk next to the tool
    install so it survives backend restarts."""
    cache_file = BASE_DIR / "tools" / "realport_probe.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())["result"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    probe_src = (
        "module src(output real r); initial r = 1.25; endmodule\n"
        "module top; real x; src u(.r(x)); "
        'initial #1 if (x == 1.25) $display("REALPORT_OK"); endmodule\n'
    )
    result = "failed"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "probe_realport.sv"
            src.write_text(probe_src)
            out = Path(td) / "p"
            c = subprocess.run([iverilog, "-g2012", "-o", str(out), str(src)],
                               capture_output=True, text=True, timeout=30)
            if c.returncode == 0:
                r = subprocess.run([vvp, str(out)], capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and "REALPORT_OK" in r.stdout:
                    result = "ok"
    except (OSError, subprocess.SubprocessError):
        result = "failed"
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"result": result}))
    except OSError:
        pass
    return result


def toolchain_status(refresh: bool = False) -> dict[str, Any]:
    """What model-verification tooling is actually available right now.
    Probed once per backend process (cheap `which` calls plus, at most once
    ever, the Icarus real-port probe); drives both the prompt contract
    (Circuit_Builder is told what it can and cannot verify) and the UI's
    inline "will be generated but unverified" warnings."""
    global _toolchain_cache
    if _toolchain_cache is not None and not refresh:
        return _toolchain_cache
    openvaf = _which("openvaf")
    iverilog = _which("iverilog")
    vvp = _which("vvp")
    ngspice = _which("ngspice")
    realport = None
    if iverilog and vvp:
        realport = _probe_realport(iverilog, vvp)
    _toolchain_cache = {
        "openvaf": {
            "available": openvaf is not None,
            "path": openvaf,
            "note": None if openvaf else
            "openvaf (OpenVAF-reloaded build) not found - install it into "
            f"{TOOLS_BIN} to enable Verilog-A compile/verify; until then "
            "Verilog-A models are generated_unverified.",
        },
        "iverilog": {
            "available": iverilog is not None and vvp is not None,
            "path": iverilog,
            "note": None if (iverilog and vvp) else
            "iverilog/vvp (Icarus Verilog) not installed and sudo needs a "
            "password on this machine - run `sudo apt install iverilog` "
            "yourself to enable Verilog/RNM verification; until then those "
            "models are generated_unverified.",
        },
        "ngspice": {"available": ngspice is not None, "path": ngspice, "note": None},
        # "ok" | "failed" | None (= not probed because iverilog is missing).
        "realport_probe": realport,
    }
    return _toolchain_cache


# ---------------------------------------------------------------------------
# Per-topology model guidance (feature spec section b tables, condensed into
# prompt text). Keys are topologies.py topology values.
# ---------------------------------------------------------------------------

_VA_GUIDANCE: dict[str, str] = {
    "ctle": "1-zero/2-pole differential stage: DC gain, peak at Nyquist (data_rate_gbps/2), "
            "smooth tanh output clamp, rout + cload. Parameters from spec fields: dc_gain_db, "
            "peaking_db, data_rate_gbps, cload_ff. TB check (AC sweep): low-frequency gain within "
            "1 dB of dc_gain_db AND gain(Nyquist)-gain(DC) within 1 dB of peaking_db. Overdrive "
            "check (tran, same .control script): drive a large sine at 3x the amplitude that "
            "would produce full output swing at the peak gain; verify |V(out)| stays <= the "
            "clamp level (the model's swing parameter) within 5% - a model missing its tanh "
            "clamp must fail this.",
    "vga": "Gain set by a gain_code parameter: gain_db = gain_min_db + code*gain_step_db; single "
           "pole at bandwidth_ghz; tanh clamp at output_swing_vpp. Parameters: gain_min_db, "
           "gain_max_db, gain_step_db, bandwidth_ghz, output_swing_vpp, cload_ff. TB check (AC at "
           "min and max code): gain within 1 dB of setting; -3 dB point within 20% of "
           "bandwidth_ghz. Overdrive check (tran, same .control script): at max gain drive a "
           "large sine at 3x the amplitude that would produce full output swing; verify |V(out)| "
           "stays <= output_swing_vpp/2 within 5% - a model missing its tanh clamp must fail this.",
    "tia": "I-in/V-out transimpedance with a single pole; input node capacitance; output clamp "
           "for max_input_current_ua. Parameters: transimpedance_gain_kohm, bandwidth_ghz, "
           "input_capacitance_pf, max_input_current_ua. TB check (AC, current source in): Zt at DC "
           "within 10% of target; -3 dB within 20%.",
    "input_termination_buffer": "Input resistance = termination_impedance_ohm to a common-mode "
           "node; unity buffer with a pole. Parameters: termination_impedance_ohm, bandwidth_ghz, "
           "gain_db. TB check: DC input resistance within 5% of target; AC gain ~gain_db.",
    "opamp_ota": "Classic 1-pole gm/rout integrator: DC gain, GBW, slew limit (tanh-limited "
           "output current), swing clamp; dominant pole = GBW/(10^(dc_gain_db/20)). State in the "
           "header that phase margin is NOT represented (single-pole model, PM = 90 deg by "
           "construction). Parameters: dc_gain_db, gbw_mhz, cload_pf, output_swing_vpp. TB check "
           "(AC open loop): DC gain within 2 dB; unity-gain frequency within 20% of gbw_mhz. "
           "Transient large-step check (same .control script): output ramp rate matches the "
           "model's slew parameter within 20% (AC linearizes, so slewing is invisible to the AC "
           "check alone). Overdrive check: verify |V(out)| stays <= output_swing_vpp/2 within 5% "
           "under a 3x-overdrive input - a model missing its clamp must fail this.",
    "bandgap_reference": "V(out) <+ vref*(1 + tc*(T-27)*1e-6) plus a vdd-sensitivity term per "
           "psrr_db; uses $temperature. Parameters: output_voltage_v, temp_coefficient_ppm_per_c, "
           "psrr_db. TB check (DC temperature sweep over temp_range_c): mean Vref within 2% of "
           "target; measured tempco within 2x of the spec value. PSRR check (DC vdd sweep, same "
           ".control script): dVref/dVdd <= 10^(-psrr_db/20) within 2x - the vdd-sensitivity term "
           "must actually be exercised, not just present.",
    "ldo": "vout = smooth-min(output_voltage_v, V(vdd) - dropout_mv/1000.0) - dropout_mv is in mV "
           "and node voltages are in volts, so the /1000.0 conversion is mandatory; finite rout so "
           "load current causes small droop; supply ripple attenuated per psrr_db_at_1khz. "
           "Parameters: output_voltage_v, max_load_current_ma, dropout_mv, psrr_db_at_1khz. TB "
           "check (DC sweep of Vin): regulation region flat within 1%; dropout onset within 20% of "
           "spec. PSRR check (AC, same .control script): V(out)/V(vdd ripple) at 1 kHz within 6 dB "
           "of -psrr_db_at_1khz - the ripple-attenuation term must actually be exercised, not just "
           "present.",
    "bias_current_mirror": "I(out) <+ Iout_target * tanh(3*V(out)/Vcompliance) - current source "
           "with smooth compliance rolloff, one branch per output (the factor 3 matters: tanh(3) = "
           "0.995, so the current is within 0.5% of target AT the compliance voltage; a plain "
           "tanh(V/Vc) is still 24% low there and cannot meet a percent-level matching check). "
           "Parameters: output_current_ua, compliance_voltage_v, num_output_branches. TB check (DC "
           "sweep of V(out) from 0 to VDD): Iout within current_matching_accuracy_pct of target "
           "for ALL V(out) >= compliance_voltage_v, AND Iout < 80% of target at V(out) = "
           "compliance_voltage_v/3 (confirms the compliance rolloff exists - a degenerate "
           "always-on current source must fail this second clause).",
    "nmos_current_mirror": "I(out) <+ Iout_target * tanh(3*V(out)/Vcompliance) - current source "
           "with smooth compliance rolloff (the factor 3 matters: tanh(3) = 0.995, so the current "
           "is within 0.5% of target AT the compliance voltage; a plain tanh(V/Vc) is still 24% "
           "low there and fails a percent-level check). Iout_target = iref_ua * ratio_out/ratio_ref "
           "from the run spec; default the compliance parameter to the Vds headroom the "
           "transistor-level design actually achieved. TB check (DC sweep of V(out) from 0 to "
           "VDD): Iout within 5% of target for ALL V(out) >= the compliance voltage, AND Iout < "
           "80% of target at V(out) = compliance/3 (confirms the rolloff exists - a degenerate "
           "always-on current source must fail this second clause).",
    "output_driver": "Static limiting output stage (the un-clocked analog half of the driver; the "
           "bit-level timing lives in the Verilog/RNM models): V(mid) <+ (output_swing_vpp/2) * "
           "tanh(k*V(in)) behind a SERIES resistance rout = output_impedance_ohm to the output "
           "pin, so the impedance spec is actually represented - this is the only model level "
           "that can capture it. Driven by an ordinary PULSE/sine source in SPICE. Parameters: "
           "output_swing_vpp, output_impedance_ohm. TB check: DC sweep measures small-signal "
           "output resistance within 10% of output_impedance_ohm; transient measures swing into a "
           "matched 50-ohm (or output_impedance_ohm) termination within 5% of the expected "
           "divided swing.",
}

_V_GUIDANCE: dict[str, str] = {
    "comparator": "Clocked D-decision: q <= d on the clock edge with a clk-to-q delay parameter "
           "CLK2Q_PS (from propagation_delay_ps). Calibration/control ports retained but "
           "functionally inert (document that). TB: drive a pattern, check the q sequence and that "
           "the measured delay matches the parameter within 0.1% or +/- 2 fs, whichever is larger "
           "(never exact equality - fs quantization).",
    "sense_amp_slicer": "Clocked D-decision: q <= d on the clock edge with CLK2Q_PS from "
           "regeneration_time_ps. Calibration ports inert (documented). TB: pattern + measured "
           "delay within 0.1% or +/- 2 fs (whichever is larger) of the parameter.",
    "sampler": "Flop with an aperture-delay parameter; TB clock from sampling_rate_gsps. TB: "
           "pattern retiming check.",
    "pll": "Output clock generator (always #(HALF_PERIOD_PS)) from output_freq_ghz; divider to a "
           "fb clock (N = output/ref ratio - integer-check it and error out otherwise); lock flag "
           "asserted LOCK_CYCLES after enable, LOCK_TIME ~= 4/(2*pi*loop_bandwidth) (first-order "
           "settling to ~2%) mapped to cycles. TB: measure the output period over 100 cycles - "
           "within 0.1% or +/- 2 fs (whichever is larger) of 1/output_freq_ghz, never exact "
           "equality (fs quantization); check lock flag timing. State in a header comment that a "
           "Verilog PLL locks 'instantly' by design.",
    "cdr": "Recovered-clock generator at data_rate_gbps plus a data retimer flop; "
           "loop_bandwidth_mhz maps to a lock-time parameter. TB: retimed data matches a delayed "
           "copy of the input pattern.",
    "dll_phase_interpolator": "Phase-select delay line: out = clk delayed by code*phase_step_ps. "
           "Parameters: clock_freq_ghz, num_phases, phase_step_ps. TB: sweep the code, measure "
           "delay within 0.1% or +/- 2 fs (whichever is larger) of code*step.",
    "clock_buffer_distribution": "fanout_loads output copies, each delayed by BUF_DELAY_PS; duty "
           "cycle preserved. Parameters: clock_freq_ghz, fanout_loads, duty_cycle_pct. TB: all "
           "outputs toggle; measured period/duty correct.",
    "serializer": "parallel_width_bits-bit shift register, load on the word clock, shift at the "
           "serial rate (serial clock derived or input). Parameters: parallel_width_bits, "
           "serial_data_rate_gbps, parallel_clock_freq_mhz. TB: known word in, serial pattern out, "
           "bit order asserted.",
    "deserializer": "Mirror of the serializer plus a word-alignment counter. Parameters: "
           "serial_data_rate_gbps, parallel_width_bits. TB: loopback with a fixed known pattern.",
    "output_driver": "Non-inverting buffer with rise/fall as separate delay parameters (from "
           "rise_fall_time_ps). TB: edge-to-edge delay check.",
}

_RNM_CLASS_OF: dict[str, str] = {}
for _t in ("ctle", "vga", "tia", "opamp_ota", "input_termination_buffer"):
    _RNM_CLASS_OF[_t] = "amp"
for _t in ("comparator", "sense_amp_slicer", "sampler"):
    _RNM_CLASS_OF[_t] = "decision"
for _t in ("pll", "cdr", "dll_phase_interpolator"):
    _RNM_CLASS_OF[_t] = "phase_loop"
for _t in ("bandgap_reference", "ldo", "bias_current_mirror", "nmos_current_mirror"):
    _RNM_CLASS_OF[_t] = "reference"
for _t in ("ffe", "dfe"):
    _RNM_CLASS_OF[_t] = "equalizer"
for _t in ("serializer", "deserializer"):
    _RNM_CLASS_OF[_t] = "serdes"
_RNM_CLASS_OF["clock_buffer_distribution"] = "clock_buffer"
_RNM_CLASS_OF["output_driver"] = "driver"

_RNM_GUIDANCE: dict[str, str] = {
    "amp": "Sampled-data gain + discrete 1-2 pole filter + saturation (VGA: gain-code port; TIA: "
           "the input is a real current, gain is Zt). TB checks: DC gain within 1 dB (TIA: Zt "
           "within 10%); response at the pole frequency -3 dB +/- 1 dB - this catches sample-tick "
           "aliasing, THE classic RNM bug (measure it by driving a sine at the pole frequency for "
           ">= 20 cycles and comparing the steady-state output/input amplitude ratio; do NOT "
           "attempt an FFT). Overdrive check: feed large-amplitude samples at 3x the input that "
           "would produce full output swing at the set gain; verify |out| stays <= the saturation "
           "limit within 5% - a model missing its saturation must fail this.",
    "decision": "On the clock edge: q <= (vin + offset_mv/1000.0) > vth; sensitivity dead-band "
           "REQUIRED whenever min_input_sensitivity_mv is specified: for |vin - (vth - offset)| < "
           "min_input_sensitivity_mv/1000.0, hold the previous decision (deterministic v1 stand-in "
           "for the metastability window; state in the header that true metastability/random "
           "resolution is not modeled); clk-to-q delay parameter. TB: sweep vin across the "
           "threshold - the decision flips at vth-offset within min_input_sensitivity_mv; delay "
           "within 0.1% or +/- 2 fs (whichever is larger) of the parameter.",
    "phase_loop": "Phase-DOMAIN model: real state variables freq_hz and phase stepped toward lock "
           "by a first-order (or second-order) discrete loop whose time constant comes from "
           "loop_bandwidth_mhz; output edges generated from the phase accumulator; lock flag when "
           "frequency error < 0.1%; jitter: perturb each output edge INDEPENDENTLY (synchronous "
           "injection) by a Gaussian sample of sigma = the rms jitter spec field around the "
           "loop-corrected ideal edge time - do NOT accumulate jitter per period (a locked loop's "
           "spec field is the integrated stationary rms; accumulation random-walks as sqrt(N) and "
           "fails the stddev check below; it is only for free-running oscillators, none in v1). Do "
           "NOT model the VCO waveform. TB: measured mean period == target within 0.1%; lock time "
           "within 2x of 4/(2*pi*loop_BW); measured edge-time stddev within 20% of the rms jitter "
           "parameter over >= 1000 edges.",
    "reference": "Real-valued outputs: vref(temp) polynomial from the tempco; LDO: vout = "
           "min(vset, vin - dropout_mv/1000.0) (dropout_mv is in mV, voltages in volts - the "
           "/1000.0 conversion is mandatory) + first-order load-step settling + droop = "
           "Iload*rout; mirror: real iout per branch. TB: temperature-argument sweep -> tempco "
           "within 2x; load step settles to within 1% at a settling tau the model itself DERIVES "
           "AND DOCUMENTS in its header comment (e.g. from the load cap and the rout judgment "
           "value - no spec field defines tau, so state the number the TB checks against); "
           "dropout onset where specified.",
    "equalizer": "FFE: UI-spaced real delay line, out = sum(tap_i * x[n-i]), taps as a real "
           "parameter array. DFE: comparator RNM + real feedback sum of the last num_taps "
           "decisions * weights. TB: FFE - impulse in, tap weights read back exactly; DFE - a "
           "canned ISI pattern corrected (known-answer test).",
    "serdes": "Bits + real timing (UI derived from the serial rate); same function as the digital "
           "Verilog level but edges placed on a real time grid. TB: loopback known-answer test.",
    "clock_buffer": "Real-time edge propagation: each of fanout_loads outputs is the input clock "
           "delayed by a real buffer delay, duty cycle preserved, independent per-edge "
           "(synchronous) jitter from the additive-jitter spec field. TB: measured period/duty "
           "correct on every output; edge-time stddev within 20% of the jitter parameter.",
    "driver": "Bit in -> real out swinging +/- output_swing_vpp/2 with linear-ramp edges of "
           "rise_fall_time_ps (piecewise updates on the sample tick). TB: swing and 20-80% "
           "rise/fall time checks.",
    "custom": "Best-effort: model the block's essential input/output function with real-valued "
           "ports and events, per the user's spec fields/notes; state clearly in the header what "
           "is and is not captured. TB: at least one self-checking known-answer test of the core "
           "function.",
}

# Verbatim restriction lists (feature spec b1/b3 - hard compile-compatibility
# constraints, lifted into the prompt unchanged).
VA_RESTRICTIONS = """Mandatory Verilog-A coding restrictions (or OpenVAF will not compile it):
- Allowed: `analog begin ... end` contribution statements (`V(out) <+ ...`,
  `I(out) <+ ...`), `ddt()`, `ddx()`, `limexp()`, `$temperature`, math functions
  (`tanh`, `ln`, `exp`, `pow`), parameters with ranges, internal nodes,
  `@(initial_step)`.
- Forbidden (unsupported by OpenVAF): `@(cross(...))`, `@(timer(...))`,
  `transition()`, `slew()`, `absdelay()`, `laplace_nd/laplace_zp/zi_*` filters,
  `$abstime`-driven event logic, digital (non-analog) blocks, bit shifts in
  analog blocks.
- Poles/zeros must be built as internal-node state equations with `ddt()`,
  not `laplace_*`. Pattern for one pole at `wp` rad/s (the standard idiom -
  use the CURRENT-contribution form on the internal node):
  `I(int) <+ gain*V(in) - V(int) - ddt(V(int))/wp;  V(out) <+ V(int);`
  Node `int` has ONLY these contributions; the `-V(int)` term acts as a 1-ohm
  normalizing conductance, so KCL gives exactly V(int)*(1 + s/wp) =
  gain*V(in): DC gain = gain, -3 dB at wp. The equivalent implicit voltage
  form `V(int) <+ gain*V(in) - ddt(V(int))/wp;` (with NO `-V(int)` term) is
  also correct. Do NOT write the voltage form WITH a `-V(int)` term
  (`V(out) <+ gain*V(in) - V(out) - ...`) - that halves the DC gain and
  doubles the pole. (A zero is added by summing a scaled `ddt(V(in))` term
  into the same equation.)
- All limiting/clipping must be smooth (`tanh`-based), never `if (V(a)>x)`
  branch switches on signal voltages - discontinuous branches are the #1
  Verilog-A convergence trap in a Newton-Raphson simulator.
- Every model must present finite input/output impedance (e.g. `rin`, `rout`,
  `cout` parameters with defaults), never an ideal 0-ohm voltage contribution
  driving an arbitrary load - the other classic convergence/singular-matrix trap.
- Do NOT model in v1: device noise (`white_noise`/`flicker_noise`), mismatch/
  Monte Carlo, startup transients of references."""

RNM_RULES = """RNM modeling rules (hard constraints, not style advice):
- `timescale 1ps/1fs` in every file. Jitter/phase-step parameters in ps can be
  fractional; with coarser precision they quantize to zero silently.
- Event-driven models only evaluate on events. Continuous dynamics (a CTLE
  pole, LDO settling) need a model sample clock: a free-running internal
  `always #TS` tick with TS <= 1/(20 x highest pole) or 0.25 UI, whichever is
  smaller, and the discrete one-pole update
  `y <= y + (1.0 - $exp(-TS_s*wp))*(x - y)` where `TS_s = TS_ps * 1e-12` (the
  tick converted to SECONDS) and `wp = 2*pi*f_pole_hz` in rad/s (e.g.
  2*pi*bandwidth_ghz*1e9). Do these unit conversions ONCE into local reals;
  NEVER put a ps quantity directly in the exponent - a ps-for-seconds mixup
  makes the exponent ~1e12x wrong and the filter silently degenerates to
  all-pass or frozen. Undersampling this tick aliases the frequency response
  - the TB must check the response at Nyquist, which catches it.
- Directional signal flow only: an RNM output tells the next block a value; it
  does not present an impedance. Do not attempt bidirectional termination /
  reflection modeling (out of scope at this level; note it in the model header).
- Jitter: model as Gaussian edge-time perturbation using
  `$dist_normal(seed,0,sigma_fs)` divided down to ps. PLL/CDR outputs (locked
  loops): perturb each output edge INDEPENDENTLY (synchronously) by a Gaussian
  sample of sigma = the rms-jitter spec field, around the loop-corrected ideal
  edge time - this directly represents the *integrated* rms jitter that spec
  field means, and its measured stddev is stationary. Accumulating
  (per-period-summed) jitter is only for free-running oscillators, none of
  which are in the v1 catalog - do NOT use it (a random walk grows as sqrt(N)
  and fails any stddev check). Buffers/dividers likewise use independent
  per-edge injection. The seed must be a module parameter so runs are
  reproducible; note `$dist_normal` updates its seed argument by reference, so
  the parameter cannot be passed directly - copy the seed parameter into an
  `integer` variable once at time 0 and pass that variable.
- PLL/CDR are phase-domain models (real `freq_hz`/`phase` state, no VCO
  waveform).
- Every testbench MUST contain a global watchdog timeout:
  `initial begin #WATCHDOG_PS; $display("RNM_TB_FAIL watchdog timeout"); $finish; end`
  with WATCHDOG_PS ~ 20x the expected test duration. A silent hang is never an
  acceptable failure mode - a stuck model (e.g. a #0 period from a bad
  parameter formula) must print the fail token and exit, not stall vvp forever.
- What NOT to model: sub-sample waveform shape, absolute accuracy below ~1%
  (that is Verilog-A/SPICE territory), supply nets as analog (a plain
  `real vdd_v` parameter is enough in v1)."""

VERILOG_RULES = """Digital Verilog rules:
- `timescale 1ps/1fs` in every file (GHz-class periods need sub-ps precision;
  with 1ns/1ps a 250 ps half-period quantizes badly and duty/jitter parameters
  silently truncate to zero).
- Captures: pin-accurate ports (data, clocks, resets, enable, digital control
  words), clocked function, delays as plain parameters in ps (#(DELAY_PS)).
- Must NOT attempt: voltages, offsets in mV, metastability randomness, jitter
  (an event-driven scheduler has no analog noise; faking jitter here creates
  false confidence), loop dynamics (a Verilog PLL locks "instantly" - that is
  fine and should be stated in a header comment).
- Every testbench MUST contain a global watchdog timeout:
  `initial begin #WATCHDOG_PS; $display("V_TB_FAIL watchdog timeout"); $finish; end`
  with WATCHDOG_PS ~ 20x the expected test duration. A silent hang is never an
  acceptable failure mode - a stuck model (e.g. a #0 period from a bad
  parameter formula) must print the fail token and exit, not stall vvp forever.
- Timing checks must use a tolerance, never exact equality: periods/delays
  "within 0.1% or +/- 2 fs, whichever is larger" - under `timescale 1ps/1fs` a
  non-round frequency (e.g. a 3 GHz half-period of 166.6667 ps) quantizes to
  the fs grid, so a literal `==` fails from quantization alone, not model
  error."""


def deliverable_files(block: str, model_type: str) -> dict[str, str]:
    """Fixed deliverable filenames per the contract (spec section c)."""
    if model_type == "veriloga":
        return {"file": f"{block}.va", "testbench": f"{block}_va_tb.spice", "sim_log": "va_sim.log"}
    if model_type == "verilog":
        return {"file": f"{block}.v", "testbench": f"{block}_v_tb.v", "sim_log": "v_sim.log"}
    return {"file": f"{block}_rnm.sv", "testbench": f"{block}_rnm_tb.sv", "sim_log": "rnm_sim.log"}


def _guidance(topology: str, model_type: str) -> str:
    if model_type == "veriloga":
        return _VA_GUIDANCE.get(topology, _RNM_GUIDANCE["custom"])
    if model_type == "verilog":
        return _V_GUIDANCE.get(topology, _RNM_GUIDANCE["custom"])
    return _RNM_GUIDANCE[_RNM_CLASS_OF.get(topology, "custom")]


def build_models_prompt_section(topology: str, requested: list[str]) -> str:
    """The "Behavioral models" section appended to the Circuit_Builder prompt
    when any model checkbox was set. Includes only the requested types, each
    with its deliverable filenames, per-block modeling guidance, verification
    commands + pass-token protocol, and the summary.json contract."""
    if not requested:
        return ""
    tools = toolchain_status()
    block = topology
    parts: list[str] = [
        "\n## Behavioral models (requested by the user - produce these IN ADDITION "
        "to the transistor-level design above, in this same directory)",
        "Every model parameter must default to the value the transistor-level "
        "design ACTUALLY ACHIEVED (per your ngspice measurements), falling back "
        "to the target spec value for anything not measurable - the model must "
        "describe the circuit you built, not the ideal spec (e.g. if the "
        "schematic only achieved 8 dB peaking vs a 10 dB target, set the model "
        "parameter to 8 dB and note it). Where the user left a spec field "
        "blank, use the same judgment value as the transistor-level design and "
        "keep the two consistent.",
        "Every model file must start with a header comment block: block name, "
        "date, abstraction level, a parameter <-> spec-field mapping table, "
        "what is NOT modeled, and verification status.",
        "Every testbench must be SELF-CHECKING: it computes its checks itself "
        "and prints exactly one machine-greppable token (listed per type "
        "below). Run the verification commands yourself and report honestly "
        "what happened - same rule as reporting exactly what ngspice measured.",
    ]

    if "veriloga" in requested:
        files = deliverable_files(block, "veriloga")
        openvaf_ok = tools["openvaf"]["available"]
        parts.append(f"""### Verilog-A model (continuous-time, runs inside ngspice via OSDI)
Deliverables (exact filenames): `{files['file']}` (the model), `{block}.osdi`
(compiled object, only if openvaf is available), `{files['testbench']}`
(self-checking ngspice testbench), `{files['sim_log']}` (its output).
What this model must capture for a {topology}: {_guidance(topology, 'veriloga')}

{VA_RESTRICTIONS}

Verification commands (run them yourself):
```
openvaf {files['file']}            # exit 0 and {block}.osdi exists = compile pass
ngspice -b {files['testbench']}    # exit 0 AND prints VA_TB_PASS; save output to {files['sim_log']}
```
ngspice OSDI usage on this machine (probed - do it exactly this way):
- Load the compiled model with `pre_osdi ./{block}.osdi` inside the `.control`
  block (this ngspice has NO `.osdi` dot-card; `pre_osdi` runs before circuit
  parsing so `N`-prefixed instances in the same file resolve).
- Instances need a model card: `.model <mname> <module_name> <param>=<value> ...`
  plus an instance line `N<inst> <node list> <mname>`. Parameters directly on
  the instance line DO NOT PARSE.
- The `.control` script computes the checks (via `meas`/`let`/`print`) and
  echoes exactly one of `VA_TB_PASS` or `VA_TB_FAIL <reason>`.
Pass criterion: exit code 0 AND the literal token VA_TB_PASS in {files['sim_log']}
AND no VA_TB_FAIL.""" + (
            "" if openvaf_ok else
            "\nNOTE: `openvaf` is NOT installed on this machine right now. Still write "
            f"`{files['file']}` and `{files['testbench']}` (respecting every restriction "
            "above), skip compilation/simulation, and set this model's status to "
            '"generated_unverified" with status_reason "openvaf not installed".'))

    iverilog_ok = tools["iverilog"]["available"]
    iverilog_note = (
        "" if iverilog_ok else
        "\nNOTE: `iverilog`/`vvp` are NOT installed on this machine right now. Still "
        "write the model and testbench files (respecting every rule above), skip "
        'compile/run, and set this model\'s status to "generated_unverified" with '
        'status_reason "iverilog not installed".')

    if "verilog" in requested:
        files = deliverable_files(block, "verilog")
        parts.append(f"""### Digital Verilog model (event-driven, bit-level)
Deliverables (exact filenames): `{files['file']}`, `{files['testbench']}`
(self-checking), `{files['sim_log']}`.
What this model must capture for a {topology}: {_guidance(topology, 'verilog')}

{VERILOG_RULES}

Verification commands (run them yourself):
```
iverilog -g2012 -o {block}_v_tb.vvp {files['file']} {files['testbench']}   # exit 0 = compile pass
vvp {block}_v_tb.vvp                                                       # exit 0 AND prints V_TB_PASS; save output to {files['sim_log']}
```
The testbench must be self-checking
(`if (expected !== got) begin $display("V_TB_FAIL <reason>"); $finish; end`)
and end with `$display("V_TB_PASS"); $finish;`.
Pass criterion: exit 0 AND literal V_TB_PASS in {files['sim_log']} AND no V_TB_FAIL.{iverilog_note}""")

    if "rnm" in requested:
        files = deliverable_files(block, "rnm")
        realport = tools.get("realport_probe")
        if realport == "ok":
            port_mode = ("This machine's Icarus build PASSED the real-port probe: use plain "
                         "`input real` / `output real` variable-type ports throughout.")
        elif realport == "failed":
            port_mode = ("This machine's Icarus build FAILED the real-port probe: encode real "
                         "values on ports as `wire [63:0]` IEEE-754 bits via `$realtobits`/"
                         "`$bitstoreal` at each module boundary (fully portable Verilog-2005).")
        else:
            port_mode = ("The Icarus real-port probe has not run (iverilog missing): default to "
                         "plain `input real` / `output real` variable-type ports - legal "
                         "SystemVerilog and the intended v1 style.")
        parts.append(f"""### RNM - real-number model (SystemVerilog, real-valued ports)
Deliverables (exact filenames): `{files['file']}`, `{files['testbench']}`
(self-checking), `{files['sim_log']}`.
Style: SystemVerilog modules whose ports are variable type `real`
(`input real`, `output real`) - NOT `nettype real` and NOT Verilog-AMS `wreal`
(neither is supported by Icarus Verilog). {port_mode}
What this model must capture for a {topology}: {_guidance(topology, 'rnm')}

{RNM_RULES}

Verification commands (run them yourself):
```
iverilog -g2012 -o {block}_rnm_tb.vvp {files['file']} {files['testbench']}
vvp {block}_rnm_tb.vvp    # exit 0 AND prints RNM_TB_PASS; save output to {files['sim_log']}
```
Pass criterion: exit 0 AND literal RNM_TB_PASS in {files['sim_log']} AND no RNM_TB_FAIL.{iverilog_note}""")

    example_entries = []
    for m in requested:
        files = deliverable_files(block, m)
        example_entries.append(
            f'     "{m}": {{"file": "{files["file"]}", "testbench": "{files["testbench"]}",\n'
            f'       "sim_log": "{files["sim_log"]}",\n'
            f'       "status": "verified" | "compile_only" | "generated_unverified" | "failed",\n'
            f'       "status_reason": "<why, e.g. \'openvaf not installed\' or the compile error>",\n'
            f'       "checks": {{"<spec_field>": {{"target": <number>, "model_measured": <number or null>}}, ...}}}}'
        )
    parts.append("""### summary.json addition for the models
Add ONE new top-level key `"models"` to the summary.json described above,
containing ONLY the model types requested this run:
   "models": {
""" + ",\n".join(example_entries) + """
   }
Status semantics (be honest - another program cross-checks the files and pass
tokens on disk and will downgrade a false "verified" to "failed"):
- "verified" = compiled AND the self-checking testbench ran and printed its
  pass token;
- "compile_only" = compiled but the testbench could not be run;
- "generated_unverified" = the required tool is missing on this machine (say
  which in status_reason);
- "failed" = compile or testbench failed (put the error in status_reason).
"checks" maps the spec fields this model's TB verified to {target,
model_measured} pairs, using the SAME field names as target_spec/measured so
the UI can show spec target vs SPICE-measured vs model-measured side by side.""")

    return "\n\n".join(parts) + "\n"


def check_models_in_summary(
    summary: dict[str, Any], run_dir: Path, requested: list[str] | None
) -> list[str]:
    """Post-run honesty pass over summary.json's "models" key (mutates
    `summary` in place; returns a list of problem strings for the run
    record's notes/reason).

    - Every requested type must be reported; a missing one becomes a
      "failed" entry.
    - Claimed model/testbench files must exist on disk, else downgrade to
      "failed".
    - A "verified" claim must be backed by the pass token (and no fail
      token) in the named sim log, else downgrade to "failed".
    """
    problems: list[str] = []
    requested = requested or []
    if not requested:
        return problems
    models = summary.get("models")
    if not isinstance(models, dict):
        models = {}
        summary["models"] = models
    for mtype in requested:
        entry = models.get(mtype)
        if not isinstance(entry, dict):
            models[mtype] = {
                "file": None, "testbench": None, "sim_log": None,
                "status": "failed",
                "status_reason": "requested model type missing from summary.json",
                "checks": {},
            }
            problems.append(f"requested {mtype} model was not reported in summary.json")
            continue
        missing = []
        for key in ("file", "testbench"):
            fname = entry.get(key)
            if fname and not (run_dir / fname).exists():
                missing.append(fname)
        if missing:
            entry["status"] = "failed"
            entry["status_reason"] = (
                f"claimed file(s) missing on disk: {', '.join(missing)}"
                + (f" (was: {entry.get('status_reason')})" if entry.get("status_reason") else "")
            )
            problems.append(f"{mtype} model file(s) missing on disk: {', '.join(missing)}")
            continue
        if not entry.get("file"):
            entry["status"] = "failed"
            entry["status_reason"] = "no model file reported"
            problems.append(f"{mtype} model reported without a file name")
            continue
        # A veriloga claim of having compiled ("verified" or "compile_only")
        # must be backed by the .osdi object on disk (cycle-3 audit item 13).
        if mtype == "veriloga" and entry.get("status") in ("verified", "compile_only"):
            claimed = entry.get("status")
            osdi = run_dir / (Path(entry["file"]).stem + ".osdi")
            if not osdi.exists():
                entry["status"] = "failed"
                entry["status_reason"] = (
                    f"claimed {claimed} but compiled object {osdi.name} does not exist on disk"
                )
                problems.append(f"veriloga model claimed {claimed} without {osdi.name} on disk")
                continue
        if entry.get("status") == "verified":
            token = PASS_TOKENS[mtype]
            fail_token = FAIL_TOKENS[mtype]
            log_name = entry.get("sim_log")
            log_path = (run_dir / log_name) if log_name else None
            log_text = ""
            if log_path is not None and log_path.exists():
                try:
                    log_text = log_path.read_text(errors="replace")
                except OSError:
                    log_text = ""
            if token not in log_text or fail_token in log_text:
                entry["status"] = "failed"
                entry["status_reason"] = (
                    f"claimed verified but {log_name or 'sim log'} does not contain "
                    f"{token} (or contains {fail_token})"
                )
                problems.append(f"{mtype} model claimed verified without a {token} in its sim log")
    return problems
