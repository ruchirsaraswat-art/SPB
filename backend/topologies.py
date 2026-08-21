"""
Canonical list of circuit topologies the spec form's dropdown offers.

Single source of truth: the frontend fetches this list from GET /api/topologies
instead of hard-coding its own copy, so the dropdown's <value> strings and the
strings `circuit_builder.build_prompt()` actually puts in front of
Circuit_Builder can never drift apart.

Only "nmos_current_mirror" has a fully fleshed-out, hand-tuned prompt template
(see circuit_builder.build_prompt) - it's the one topology this tool has
actually built, netlisted, simulated, and eyeballed the rendered schematic
for. Every other value here gets a generic prompt template that hands the
topology name + whatever spec numbers were filled in to Circuit_Builder and
trusts its own topology/sizing expertise - this tool does not (and per its
own charter should not) hard-code the internal structure of a PLL, CDR, DFE,
etc. If a topology here turns out to need topology-specific fields or a more
prescriptive prompt (the way the current mirror does), add that in
build_prompt() and extend TopologySpec's optional fields as needed - don't
speculatively build it before anyone has run it once.
"""

from __future__ import annotations

from models_contract import MODEL_APPLICABILITY

# (value, label) pairs, grouped for <optgroup> rendering. "value" is also the
# literal string interpolated into the generic Circuit_Builder prompt for any
# topology other than "nmos_current_mirror", so keep values as clean,
# unambiguous circuit-block names (not marketing copy).
TOPOLOGY_GROUPS: list[dict] = [
    {
        "group": "Front end / analog",
        "options": [
            {"value": "ctle", "label": "CTLE (continuous-time linear equalizer)"},
            {"value": "vga", "label": "VGA (variable gain amplifier)"},
            {"value": "tia", "label": "TIA (transimpedance amplifier)"},
            {"value": "input_termination_buffer", "label": "Input termination / buffer"},
        ],
    },
    {
        "group": "Equalization",
        "options": [
            {"value": "ffe", "label": "FFE (feed-forward equalizer)"},
            {"value": "dfe", "label": "DFE (decision feedback equalizer)"},
        ],
    },
    {
        "group": "Clocking",
        "options": [
            {"value": "pll", "label": "PLL (phase-locked loop)"},
            {"value": "cdr", "label": "CDR (clock and data recovery)"},
            {"value": "dll_phase_interpolator", "label": "DLL / phase interpolator (deskew, phase shift)"},
            {"value": "clock_buffer_distribution", "label": "Clock buffer / distribution"},
        ],
    },
    {
        "group": "Sampling / slicing",
        "options": [
            {"value": "sense_amp_slicer", "label": "Sense amp / slicer"},
            {"value": "comparator", "label": "Comparator"},
            {"value": "sampler", "label": "Sampler (track-and-hold)"},
        ],
    },
    {
        "group": "Serialization",
        "options": [
            {"value": "serializer", "label": "Serializer (parallel-to-serial, PISO)"},
            {"value": "deserializer", "label": "Deserializer (serial-to-parallel, SIPO)"},
            {"value": "output_driver", "label": "Output driver (CML / voltage-mode)"},
        ],
    },
    {
        "group": "General-purpose analog",
        "options": [
            {"value": "opamp_ota", "label": "Op-amp / OTA (general-purpose amplifier)"},
            {"value": "ldo", "label": "LDO (low-dropout regulator)"},
        ],
    },
    {
        "group": "Bias / reference",
        "options": [
            {"value": "bandgap_reference", "label": "Bandgap reference"},
            {"value": "bias_current_mirror", "label": "Current mirror / bias generator (general-purpose)"},
        ],
    },
    {
        "group": "Built-in (detailed sizing flow)",
        "options": [
            {
                "value": "nmos_current_mirror",
                "label": "NMOS current mirror (2-transistor - the flow this tool was originally built for)",
            },
        ],
    },
]

# Flattened value -> label, used both to serve /api/topologies metadata and
# to validate incoming spec.topology server-side.
TOPOLOGY_LABELS: dict[str, str] = {
    opt["value"]: opt["label"] for group in TOPOLOGY_GROUPS for opt in group["options"]
}

CUSTOM_TOPOLOGY_VALUE = "custom"

ALL_TOPOLOGY_VALUES: set[str] = set(TOPOLOGY_LABELS) | {CUSTOM_TOPOLOGY_VALUE}


def topology_display_name(topology: str, custom_topology: str | None) -> str:
    """Human-readable name to interpolate into a Circuit_Builder prompt."""
    if topology == CUSTOM_TOPOLOGY_VALUE:
        return (custom_topology or "").strip() or "custom circuit block (unspecified by user)"
    return TOPOLOGY_LABELS.get(topology, topology)


# Per-topology structured spec fields, replacing the one-size-fits-all
# gain_db/bandwidth_ghz pair every non-current-mirror topology used to be
# forced through. That pair was wrong or misleading for most blocks (wrong
# units for TIA - transimpedance in ohms, not a voltage-gain dB figure; no
# "gain" concept at all for PLL/CDR/serializer/DFE/bandgap; "bandwidth"
# ambiguously conflating loop bandwidth (MHz) with carrier/data rate (GHz)
# for PLL/CDR/clock-buffer/serializer). Field set and units per topology are
# from a Circuit_Researcher audit against real SerDes/PHY spec conventions -
# see ~/.claude/agent-knowledge/circuit-topologies/serdes-phy-spec-fields.md
# for the full reasoning/sources.
#
# Each entry: {"name": <RunSpec.extra_fields key>, "label": <form label>,
# "unit": <display unit, "" if unitless>, "type": "number" (default) | "text"}.
# Optional validation/feasibility keys (2026-08-20 audit, finding 5):
#   "min"/"max"           hard physical-validity bounds. Enforced server-side
#                         in validate_extra_fields() (422 on violation) and
#                         rendered as HTML min/max attributes in the form.
#   "warn_above"/"warn_below" + "warn_text"
#                         soft sky130 feasibility thresholds. NOT enforced -
#                         the form shows warn_text inline without blocking
#                         submit, so an ambitious spec still runs but the
#                         user has been told it's beyond what sky130 has
#                         demonstrably done.
#
# power_mw, area_um2, vdd_v, corner, temp_c, notes are NOT listed here - they
# stay as RunSpec/ResearchSpec's fixed, always-present fields rather than
# per-topology entries.
#
# "nmos_current_mirror" intentionally has no entry - it already has its own
# fully-specified field set (iref_ua, ratio_out, ratio_ref, w_ref_um,
# l_ref_um) handled separately in main.py/circuit_builder.py, and never used
# gain_db/bandwidth_ghz in the first place. "custom" also has no entry by
# design (no fixed field set makes sense for an arbitrary free-text block) -
# notes remains the catch-all there for the form, and API users may attach
# arbitrary free-form extra_fields keys (see validate_extra_fields: the
# unknown-key rejection deliberately does not apply to "custom", since there
# is no schema to be "unknown" against and strict rejection would make it
# impossible to express anything unusual in a structured way).

# Shared soft-feasibility annotations (audit finding 5). Real sky130 circuit
# speeds are far below the peak device fT/fmax (~63/121 GHz): published
# open-source sky130 SerDes work is ~1-2 Gb/s class and PLL/VCO work clusters
# at or below a few GHz.
#
# Parameterized by unit (cycle 1.5, follow-up c): the same 5/10 thresholds
# apply to Gb/s data-rate fields and GS/s sampling/clock-rate fields (a
# full-rate slicer clocks at the data rate), but the warn text must quote
# the field's own unit - "10 Gb/s" under a GS/s field reads like a bug.
def _warn_data_rate(unit: str = "Gb/s") -> dict:
    if unit == "GS/s":
        text = (
            "Aggressive for sky130 - published open-source sky130 SerDes work implies "
            "sampler/slicer clock rates of ~1-2 GS/s (full-rate at ~1-2 Gb/s); "
            "above 10 GS/s is unrealistic."
        )
    else:
        text = (
            "Aggressive for sky130 - published open-source sky130 SerDes work is "
            "~1-2 Gb/s class; above 10 Gb/s is unrealistic."
        )
    return {"warn_above": 5, "warn_text": text}
_WARN_CLOCK_GHZ = {
    "warn_above": 4,
    "warn_text": "Beyond published sky130 VCO/PLL/clocking results, which cluster at or "
    "below a few GHz.",
}
_PHASE_MARGIN_BOUNDS = {
    "min": 20, "max": 85, "warn_below": 45,
    "warn_text": "Below ~45 deg the loop rings and peaks jitter; 45-70 deg is the usual target.",
}
_WARN_PSRR = {
    "warn_above": 90,
    "warn_text": "PSRR above ~90 dB is not credible for a bare block at a 1.8 V supply - "
    "expect cascoding/pre-regulation, and say so in notes if you really need it.",
}

# Load capacitance (audit finding 4): bandwidth/delay/jitter specs are
# meaningless without the load the output drives, so every voltage-output
# block gets this field. (output_driver/input_termination_buffer don't -
# their load is the termination impedance.)
_CLOAD_FIELD = {
    "name": "cload_ff", "label": "Load capacitance", "unit": "fF", "min": 0,
    "help": "Capacitance each output drives; typical on-chip 5-50 fF. Delay/bandwidth/"
    "jitter specs are meaningless without it - if left blank Circuit_Builder must "
    "invent one, and the measured-vs-target comparison is against an unstated condition.",
}

TOPOLOGY_FIELDS: dict[str, list[dict]] = {
    "ctle": [
        {"name": "dc_gain_db", "label": "DC / low-frequency gain", "unit": "dB",
         "help": "Gain at low frequency, before the peaking kicks in. Often 0 dB or slightly negative for a passive/degenerated CTLE - the CTLE's job is relative boost, not absolute gain."},
        {"name": "peaking_db", "label": "Peaking (boost at Nyquist vs. DC)", "unit": "dB",
         "min": 0, "warn_above": 12,
         "warn_text": "More than ~12 dB of boost typically needs more than one CTLE stage.",
         "help": "The CTLE's headline spec: how much more gain it gives at the Nyquist frequency (data rate / 2) than at DC. Should roughly match the channel loss it must undo."},
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "The data rate being equalized. Sets Nyquist = data rate / 2, which is where the peaking should land."},
        {"name": "target_channel_loss_db_at_nyquist", "label": "Channel loss to compensate at Nyquist", "unit": "dB", "min": 0,
         "help": "Insertion loss of the channel at Nyquist that this CTLE must flatten out. Peaking plus DFE/FFE contributions should cover this number."},
        dict(_CLOAD_FIELD),
    ],
    "vga": [
        {"name": "gain_min_db", "label": "Gain, minimum", "unit": "dB",
         "help": "Lowest gain setting. A VGA's spec is a range, not one number - min matters for strong input signals that must not overload later stages."},
        {"name": "gain_max_db", "label": "Gain, maximum", "unit": "dB",
         "help": "Highest gain setting, used for the weakest input the link must handle."},
        {"name": "gain_step_db", "label": "Gain step size", "unit": "dB", "min": 0,
         "help": "Control resolution between adjacent gain settings. Finer steps ease AGC loop design but cost complexity."},
        {"name": "bandwidth_ghz", "label": "Bandwidth (held across gain range)", "unit": "GHz", "min": 0.001,
         "help": "-3 dB bandwidth, which should hold at every gain setting - the hardest corner is usually maximum gain."},
        {"name": "output_swing_vpp", "label": "Output swing", "unit": "Vpp", "min": 0,
         "help": "Linear output swing the VGA must deliver. At high gain this, not raw gain, is usually the limiting headroom/linearity spec. Cannot exceed VDD."},
        dict(_CLOAD_FIELD),
    ],
    "tia": [
        {"name": "transimpedance_gain_kohm", "label": "Transimpedance gain", "unit": "kOhm", "min": 0,
         "help": "Output voltage per input current (V/A, in ohms) - a TIA's gain is an impedance, not a dB voltage ratio. Higher gain trades directly against bandwidth."},
        {"name": "bandwidth_ghz", "label": "Bandwidth", "unit": "GHz", "min": 0.001,
         "help": "-3 dB bandwidth. Rule of thumb for NRZ: about 0.7x the data rate. Also sets how much input noise gets integrated."},
        {"name": "input_capacitance_pf", "label": "Input capacitance", "unit": "pF", "min": 0,
         "help": "Photodiode plus wiring capacitance at the input node - the dominant term in the TIA's gain-bandwidth-stability tradeoff. Get this from the photodiode datasheet."},
        {"name": "input_referred_noise_pa_per_rthz", "label": "Input-referred noise density", "unit": "pA/sqrt(Hz)", "min": 0,
         "help": "Equivalent input current noise density - the sensitivity spec. Integrated over the bandwidth it sets the minimum detectable optical signal."},
        {"name": "max_input_current_ua", "label": "Max input current", "unit": "uA", "min": 0,
         "help": "Largest photocurrent before the TIA overloads/distorts - the top of the dynamic range."},
    ],
    "input_termination_buffer": [
        {"name": "termination_impedance_ohm", "label": "Termination impedance", "unit": "Ohm", "min": 1,
         "help": "The defining spec of a termination: the impedance matching the channel, typically 50 ohm single-ended / 100 ohm differential."},
        {"name": "return_loss_db", "label": "Return loss (positive dB; larger is better)", "unit": "dB", "min": 0,
         "help": "Enter a positive dB number: return loss of 10 dB means |S11| = -10 dB, i.e. 10% of the incident power reflects. Larger is better. Poor return loss shows up as reflections/ISI."},
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Data rate the input must pass - sets the frequency range over which matching and bandwidth must hold."},
        {"name": "bandwidth_ghz", "label": "Bandwidth", "unit": "GHz", "min": 0.001,
         "help": "-3 dB bandwidth of the buffer path after the termination."},
        {"name": "gain_db", "label": "Gain (nominal ~0 dB pass-through)", "unit": "dB",
         "help": "Optional: buffers are nominally unity gain; specify only if a specific insertion gain/loss is required."},
    ],
    "ffe": [
        {"name": "num_taps", "label": "Number of taps", "unit": "", "min": 1,
         "help": "Tap count of the delay line, e.g. 3-tap = 1 pre-cursor + main + 1 post-cursor. More taps correct more ISI but cost power and swing."},
        {"name": "tap_spacing_ui", "label": "Tap spacing", "unit": "UI", "min": 0.01,
         "help": "Delay between taps in unit intervals: 1.0 = symbol-spaced (typical for TX FFE); fractional spacing gives finer control."},
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Data rate the FFE runs at - defines the UI the tap delays are built around."},
        {"name": "max_boost_db", "label": "Max pre-emphasis boost at Nyquist", "unit": "dB", "min": 0,
         "help": "Maximum high-frequency emphasis relative to DC with the taps at full de-emphasis. Comes at the cost of reduced low-frequency swing."},
    ],
    "dfe": [
        {"name": "num_taps", "label": "Number of taps", "unit": "", "min": 1,
         "help": "How many post-cursor ISI terms are fed back and cancelled. First tap is the hardest (tightest timing) and most valuable."},
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Data rate - the feedback must settle within one UI of this rate (or be unrolled)."},
        {"name": "loop_latency_ui", "label": "Loop latency (unrolled vs. full feedback)", "unit": "UI", "min": 0,
         "help": "Slicer-to-summer feedback delay in UI. Must close within 1 UI for a direct-feedback tap-1; speculative/unrolled DFEs relax this at the cost of parallel hardware."},
        {"name": "tap_weight_range_mv", "label": "Tap weight range (max correctable post-cursor ISI)", "unit": "mV", "min": 0,
         "help": "Largest ISI amplitude each tap can subtract at the summing node - sets how bad a channel the DFE can clean up."},
    ],
    "pll": [
        {"name": "ref_freq_mhz", "label": "Reference frequency", "unit": "MHz", "min": 0.001,
         "help": "Input reference clock (crystal or system clock). With the output frequency it fixes the feedback divider N."},
        {"name": "output_freq_ghz", "label": "Output frequency", "unit": "GHz", "min": 0.001, **_WARN_CLOCK_GHZ,
         "help": "VCO/output clock frequency the PLL synthesizes - note GHz here vs. MHz for the loop bandwidth; they are different quantities."},
        {"name": "loop_bandwidth_mhz", "label": "Loop bandwidth", "unit": "MHz", "min": 0.001,
         "help": "Bandwidth of the control loop (MHz, typically 1/10-1/20 of the reference). Inside it, VCO noise is suppressed; outside, reference noise is. Not the output frequency!"},
        {"name": "phase_margin_deg", "label": "Phase margin", "unit": "deg", **_PHASE_MARGIN_BOUNDS,
         "help": "Stability margin of the loop, typically 45-70 degrees. Too low rings and peaks jitter; too high slows lock."},
        {"name": "rms_jitter_ps", "label": "Integrated rms jitter", "unit": "ps", "min": 0,
         "help": "RMS jitter of the output clock integrated over the band of interest - the PLL's headline deliverable; loop bandwidth/phase margin are means to hit it."},
    ],
    "cdr": [
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Incoming serial data rate the CDR locks to (GHz-scale; distinct from the MHz-scale loop bandwidth)."},
        {"name": "loop_bandwidth_mhz", "label": "Loop bandwidth", "unit": "MHz", "min": 0.001,
         "help": "Tracking bandwidth of the recovery loop. Wider tracks more input jitter (better JTOL) but passes more of it downstream."},
        {"name": "jitter_tolerance_ui", "label": "Jitter tolerance (JTOL corner)", "unit": "UI", "min": 0,
         "help": "Sinusoidal jitter amplitude (in UI) the CDR must absorb at the JTOL mask corner frequency while keeping the BER target - a CDR's headline spec."},
        {"name": "phase_margin_deg", "label": "Phase margin", "unit": "deg", **_PHASE_MARGIN_BOUNDS,
         "help": "Stability margin of the CDR loop, same meaning as for a PLL."},
        {"name": "recovered_clock_jitter_ps", "label": "Recovered clock rms jitter", "unit": "ps", "min": 0,
         "help": "RMS jitter of the recovered clock handed to the sampler - what the downstream timing budget actually sees."},
    ],
    "dll_phase_interpolator": [
        {"name": "clock_freq_ghz", "label": "Clock frequency", "unit": "GHz", "min": 0.001, **_WARN_CLOCK_GHZ,
         "help": "Input clock the DLL locks its delay line to / the PI interpolates - e.g. the forwarded DQS strobe rate in a DDR PHY."},
        {"name": "num_phases", "label": "Number of output phases", "unit": "", "min": 2,
         "help": "How many evenly spaced clock phases are produced (e.g. 4 for 90-degree DDR shifts; PIs commonly interpolate 32-64 steps between quadrature phases)."},
        {"name": "phase_step_ps", "label": "Phase step size", "unit": "ps", "min": 0,
         "help": "Time resolution of one interpolator/deskew step = period / num_phases. Sets how finely per-lane skew and the 90-degree strobe shift can be trimmed."},
        {"name": "integral_nonlinearity_lsb", "label": "Integral nonlinearity (INL)", "unit": "LSB", "min": 0,
         "help": "Worst-case deviation of the actual phase from the ideal uniform step ladder, in steps (LSB). PI INL eats directly into the timing budget."},
    ],
    "clock_buffer_distribution": [
        {"name": "clock_freq_ghz", "label": "Clock frequency", "unit": "GHz", "min": 0.001, **_WARN_CLOCK_GHZ,
         "help": "The clock the buffer/tree must pass cleanly - the operating frequency, not a -3 dB bandwidth."},
        {"name": "duty_cycle_pct", "label": "Duty cycle target", "unit": "%", "min": 30, "max": 70,
         "help": "Output duty cycle target (usually 50%). Duty-cycle distortion directly eats timing margin in DDR/half-rate designs."},
        {"name": "additive_jitter_fs", "label": "Additive jitter (rms) budget", "unit": "fs", "min": 0,
         "help": "RMS jitter the buffer itself may add on top of its input clock - the figure of merit for clock distribution."},
        {"name": "fanout_loads", "label": "Fanout (downstream loads)", "unit": "", "min": 1,
         "help": "How many downstream loads are driven - sets device sizing and per-branch skew management."},
        {**_CLOAD_FIELD, "label": "Per-load capacitance",
         "help": "Capacitance of EACH downstream load (alongside the fanout count above); typical on-chip 5-50 fF. Delay/jitter/duty-cycle specs are meaningless without it."},
    ],
    "sense_amp_slicer": [
        {"name": "sampling_rate_gsps", "label": "Sampling / clock rate", "unit": "GS/s", "min": 0.001, **_warn_data_rate("GS/s"),
         "help": "Clock rate at which the latch regenerates a decision - one decision per clock edge."},
        {"name": "input_offset_mv", "label": "Input offset", "unit": "mV", "min": 0,
         "help": "Input-referred offset of the latch - the dominant error source; usually needs offset calibration to get under a few mV."},
        {"name": "min_input_sensitivity_mv", "label": "Minimum resolvable input", "unit": "mV", "min": 0,
         "help": "Smallest differential input reliably resolved to a full logic level within the clock period (after offset correction)."},
        {"name": "regeneration_time_ps", "label": "Regeneration time", "unit": "ps", "min": 0,
         "help": "Time the positive-feedback latch needs to amplify a small input to full swing - it bounds the maximum clock rate."},
        dict(_CLOAD_FIELD),
    ],
    "comparator": [
        {"name": "input_offset_mv", "label": "Input offset", "unit": "mV", "min": 0,
         "help": "Input-referred offset - shifts the effective decision threshold."},
        {"name": "propagation_delay_ps", "label": "Propagation delay", "unit": "ps", "min": 0,
         "help": "Input-crossing to output-switching delay at a stated overdrive - comparators are specified by delay, not -3 dB bandwidth."},
        {"name": "overdrive_mv", "label": "Minimum input overdrive", "unit": "mV", "min": 0,
         "help": "How far past the threshold the input must go for the delay spec to hold; small overdrives slow the decision."},
        {"name": "hysteresis_mv", "label": "Hysteresis (if used)", "unit": "mV", "min": 0,
         "help": "Deliberate threshold split between rising/falling decisions to reject noise chatter on slow inputs. Leave blank for none."},
        {"name": "gain_db", "label": "Open-loop DC gain (secondary)", "unit": "dB",
         "help": "Optional: open-loop gain for continuous-time comparators; usually secondary to offset and delay."},
        dict(_CLOAD_FIELD),
    ],
    "sampler": [
        {"name": "sampling_rate_gsps", "label": "Sampling rate", "unit": "GS/s", "min": 0.001, **_warn_data_rate("GS/s"),
         "help": "How many samples per second the track-and-hold takes (distinct from the input bandwidth)."},
        {"name": "input_bandwidth_ghz", "label": "Input tracking bandwidth", "unit": "GHz", "min": 0.001,
         "help": "-3 dB bandwidth while tracking - must exceed the highest input frequency of interest, often well above Nyquist for sub-sampling."},
        {"name": "aperture_jitter_fs", "label": "Aperture jitter (sampling clock)", "unit": "fs", "min": 0,
         "help": "RMS uncertainty of the sampling instant. With a high-frequency input it caps the achievable SNR: SNR = -20*log10(2*pi*fin*tj)."},
        {"name": "hold_droop_mv_per_ns", "label": "Hold droop", "unit": "mV/ns", "min": 0,
         "help": "How fast the held voltage leaks away during hold - matters when the downstream ADC/slicer needs the value for a long time."},
        dict(_CLOAD_FIELD),
    ],
    "serializer": [
        {"name": "parallel_width_bits", "label": "Parallel width", "unit": "bits", "min": 1,
         "help": "Input word width, e.g. 16:1 or 32:1 - sets the mux-tree depth and the internal clock domains."},
        {"name": "serial_data_rate_gbps", "label": "Serial data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Output line rate = parallel width x parallel word rate."},
        {"name": "parallel_clock_freq_mhz", "label": "Parallel clock frequency", "unit": "MHz", "min": 0.001,
         "help": "Word clock on the parallel side = serial rate / width. Stated explicitly to pin down the clocking plan."},
        {"name": "output_interface_type", "label": "Output interface type", "unit": "", "type": "text",
         "help": "Electrical style of the serial output, e.g. CML differential, voltage-mode differential, single-ended."},
    ],
    "deserializer": [
        {"name": "serial_data_rate_gbps", "label": "Serial data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Input line rate to be demultiplexed."},
        {"name": "parallel_width_bits", "label": "Parallel width", "unit": "bits", "min": 1,
         "help": "Output word width (1:16, 1:32...) - sets demux depth and word-clock frequency."},
        {"name": "parallel_clock_freq_mhz", "label": "Parallel clock frequency", "unit": "MHz", "min": 0.001,
         "help": "Word clock on the parallel side = serial rate / width."},
        {"name": "input_sensitivity_mvpp", "label": "Input sensitivity", "unit": "mVpp", "min": 0,
         "help": "Minimum input swing the first demux stage still resolves correctly at speed."},
    ],
    "output_driver": [
        {"name": "output_swing_vpp", "label": "Output swing", "unit": "Vpp", "min": 0,
         "help": "Swing delivered into the specified (terminated) load - the driver's primary spec, e.g. 0.8-1.2 Vpp differential for CML. Cannot exceed VDD."},
        {"name": "output_impedance_ohm", "label": "Output impedance", "unit": "Ohm", "min": 1,
         "help": "Source impedance, matched to the channel (typically 50 ohm per side) to absorb reflections."},
        {"name": "data_rate_gbps", "label": "Data rate", "unit": "Gb/s", "min": 0.001, **_warn_data_rate("Gb/s"),
         "help": "Line rate the driver must toggle at."},
        {"name": "rise_fall_time_ps", "label": "Rise/fall time (20-80%)", "unit": "ps", "min": 0,
         "help": "Edge speed into the load. Too slow closes the eye; too fast wastes power and radiates - typically ~0.3-0.4 UI."},
    ],
    "opamp_ota": [
        {"name": "dc_gain_db", "label": "Open-loop DC gain", "unit": "dB",
         "help": "Open-loop gain at DC. A single sky130 stage gives ~30-40 dB; 60-80 dB usually means two stages or cascoding (which costs headroom at 1.8 V)."},
        {"name": "gbw_mhz", "label": "Gain-bandwidth product", "unit": "MHz", "min": 0.001,
         "help": "Unity-gain frequency of the open-loop response - together with the load capacitance it sets the required gm of the input pair."},
        {"name": "phase_margin_deg", "label": "Phase margin (at unity gain)", "unit": "deg", **_PHASE_MARGIN_BOUNDS,
         "help": "Stability margin in unity-gain feedback, typically 60 deg. Below ~45 deg the step response rings; compensation (Miller cap) trades it against GBW."},
        {"name": "cload_pf", "label": "Load capacitance", "unit": "pF", "min": 0,
         "help": "Capacitance the output must drive - GBW and phase margin are meaningless without it, and it sizes the output stage/compensation."},
        {"name": "output_swing_vpp", "label": "Output swing", "unit": "Vpp", "min": 0,
         "help": "Linear output swing required. Rail-to-rail output stages get close to VDD; telescopic/cascode stages give up a few hundred mV per stacked device. Cannot exceed VDD."},
        {"name": "input_referred_noise_nv_per_rthz", "label": "Input-referred noise density", "unit": "nV/sqrt(Hz)", "min": 0,
         "help": "Equivalent input voltage noise density (flat-band). Sets the input-pair gm/current; flicker noise matters below ~1 MHz - mention the frequency of interest in notes."},
    ],
    "ldo": [
        {"name": "output_voltage_v", "label": "Output voltage", "unit": "V", "min": 0,
         "help": "The regulated supply this LDO produces - e.g. a clean 1.2-1.5 V rail for a VCO/clock path off the noisy 1.8 V supply."},
        {"name": "max_load_current_ma", "label": "Max load current", "unit": "mA", "min": 0,
         "help": "Largest current the pass device must deliver while staying in regulation - sizes the pass transistor and dominates quiescent-vs-load tradeoffs."},
        {"name": "dropout_mv", "label": "Dropout voltage", "unit": "mV", "min": 0,
         "help": "Minimum input-output headroom at max load before regulation is lost. Vdd minus dropout must exceed the output voltage."},
        {"name": "psrr_db_at_1khz", "label": "PSRR at 1 kHz", "unit": "dB", **_WARN_PSRR,
         "help": "Supply-noise rejection at low frequency - the reason to use an LDO at all for analog/clock supplies. PSRR degrades at high frequency; state the band that matters in notes."},
        {"name": "load_cap_uf_or_capless", "label": "Load cap (uF) or 'capless'", "unit": "", "type": "text",
         "help": "External/on-die output capacitor value, or 'capless' for an internally compensated design - the stability strategy differs fundamentally between the two."},
    ],
    "bandgap_reference": [
        {"name": "output_voltage_v", "label": "Target output voltage", "unit": "V", "min": 0,
         "help": "The reference voltage produced, classically ~1.2 V (silicon bandgap); sub-1V variants use current-mode summing."},
        {"name": "temp_coefficient_ppm_per_c", "label": "Temperature coefficient", "unit": "ppm/C", "min": 0,
         "warn_below": 10,
         "warn_text": "Below ~10 ppm/C needs trimming and/or curvature correction - an untrimmed first-order bandgap lands in the tens of ppm/C.",
         "help": "Vref drift over temperature - the core bandgap figure of merit. Tens of ppm/C is typical for an untrimmed first-order design. Verified with a DC temperature sweep over the range below."},
        {"name": "temp_range_c", "label": "Temperature range (e.g. -40 to 125)", "unit": "C", "type": "text",
         "help": "Temperature span over which the tempco is specified - the same design measures very differently over 0-70 vs -40-125."},
        {"name": "psrr_db", "label": "PSRR", "unit": "dB", **_WARN_PSRR,
         "help": "How well supply noise is rejected at the reference output, stated at DC or a given frequency. Matters when the supply is noisy (e.g. next to drivers)."},
    ],
    "bias_current_mirror": [
        {"name": "output_current_ua", "label": "Target output current", "unit": "uA", "min": 0,
         "help": "The bias current each output branch must deliver - the block's defining spec."},
        {"name": "num_output_branches", "label": "Number of output branches (fanout)", "unit": "", "min": 1,
         "help": "How many mirrored copies of the bias are distributed - affects area, routing, and matching strategy."},
        {"name": "compliance_voltage_v", "label": "Compliance voltage (min headroom)", "unit": "V", "min": 0,
         "help": "Minimum voltage the output branch needs across it to hold its current accurately. Critical at 1.8 V sky130 supplies - cascodes buy accuracy but cost headroom."},
        {"name": "current_matching_accuracy_pct", "label": "Current matching accuracy", "unit": "%", "min": 0, "max": 100,
         "help": "Allowed branch-to-branch (and target-to-actual) current mismatch - drives device area via mismatch scaling and layout (common-centroid, dummies)."},
    ],
}

# Inject each topology's field schema into its TOPOLOGY_GROUPS option entry
# so GET /api/topologies (which just returns TOPOLOGY_GROUPS) already carries
# everything the frontend needs to render the right inputs per topology,
# without a second endpoint or a hand-kept-in-sync frontend copy.
# Same data-driven pattern for the behavioral-model applicability matrix
# (cycle 2): each option carries "models" = {type: True | "<reason it's
# disabled>"} so the form's checkboxes (and their disabled-state tooltips)
# come straight from models_contract.MODEL_APPLICABILITY.
for _group in TOPOLOGY_GROUPS:
    for _opt in _group["options"]:
        _opt["fields"] = TOPOLOGY_FIELDS.get(_opt["value"], [])
        _opt["models"] = MODEL_APPLICABILITY.get(_opt["value"], MODEL_APPLICABILITY["custom"])


def topology_fields(topology: str) -> list[dict]:
    return TOPOLOGY_FIELDS.get(topology, [])


def validate_extra_fields(topology: str, extra_fields: dict | None) -> None:
    """Server-side guardrail for RunSpec/ResearchSpec.extra_fields (2026-08-20
    audit, findings 5 & 6). Raises ValueError (surfaced as a 422 by the
    Pydantic model validators in main.py) when:
      - a key isn't defined for this topology (typo/stale client - previously
        such keys were silently discarded by extra_fields_target_dict());
      - a numeric field's value isn't a number, or violates the field's hard
        "min"/"max" physical-validity bounds.
    Soft warn_above/warn_below feasibility thresholds are deliberately NOT
    enforced here - those are rendered as non-blocking warnings in the form.

    The "custom" topology is exempt from the unknown-key rejection (cycle
    1.5, decision on follow-up d): it has no schema, so every key would be
    "unknown" and strict rejection would leave API users no structured way
    to express an unusual block's spec. Free-form keys are accepted there
    and passed through verbatim to the prompts (see format_extra_field_lines
    / extra_fields_target_dict). Cataloged topologies stay strict.
    """
    extra_fields = extra_fields or {}
    if topology == CUSTOM_TOPOLOGY_VALUE:
        # No schema to check keys/bounds against; only reject values that
        # can't be rendered meaningfully into a prompt line.
        for name, value in extra_fields.items():
            if isinstance(value, (dict, list)):
                raise ValueError(
                    f"extra_fields.{name} must be a scalar (number or string) for the "
                    f"'custom' topology, got {type(value).__name__}"
                )
        return
    fields = {f["name"]: f for f in topology_fields(topology)}
    unknown = sorted(k for k in extra_fields if k not in fields)
    if unknown:
        valid = sorted(fields) if fields else ["(none - this topology has no structured fields)"]
        raise ValueError(
            f"unknown extra_fields key(s) for topology '{topology}': {', '.join(unknown)} - "
            f"valid keys: {', '.join(valid)}"
        )
    for name, value in extra_fields.items():
        f = fields[name]
        if value is None or value == "":
            continue
        if f.get("type") == "text":
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"extra_fields.{name} must be a number, got {value!r}") from None
        unit = f.get("unit") or ""
        unit_sfx = f" {unit}" if unit else ""
        if "min" in f and num < f["min"]:
            raise ValueError(
                f"extra_fields.{name} = {value}{unit_sfx} is below the physical minimum "
                f"{f['min']}{unit_sfx} ({f['label']})"
            )
        if "max" in f and num > f["max"]:
            raise ValueError(
                f"extra_fields.{name} = {value}{unit_sfx} is above the physical maximum "
                f"{f['max']}{unit_sfx} ({f['label']})"
            )


def format_extra_field_lines(topology: str, extra_fields: dict | None) -> list[str]:
    """Human-readable "- Label: value unit" lines for every structured field
    defined for this topology, shared by both circuit_builder.py's and
    circuit_researcher.py's prompt builders so the two don't drift. Lists
    every defined field even when unset (explicit "not specified" line) so
    Circuit_Builder/Circuit_Researcher know what was actually asked about.

    For the "custom" topology (no schema) the user's free-form keys are
    passed through verbatim - otherwise keys accepted by
    validate_extra_fields would silently vanish from the prompt, the exact
    silent-discard bug strict validation was added to prevent."""
    extra_fields = extra_fields or {}
    if topology == CUSTOM_TOPOLOGY_VALUE:
        return [
            f"- {name} (user-defined field): {value}"
            for name, value in extra_fields.items()
            if value is not None and value != ""
        ]
    lines = []
    for f in topology_fields(topology):
        value = extra_fields.get(f["name"])
        unit = f.get("unit") or ""
        if value is None or value == "":
            lines.append(f"- {f['label']}: not specified - use your own engineering judgment")
        else:
            lines.append(f"- {f['label']}: {value}{(' ' + unit) if unit else ''}")
    return lines


def extra_fields_target_dict(topology: str, extra_fields: dict | None) -> dict:
    """Plain {field_name: value_or_None} for every field defined for this
    topology - used to build the target_spec JSON contract embedded in the
    Circuit_Builder prompt, so the "measured" side of summary.json can be
    keyed the same way regardless of which topology was picked. For "custom"
    (no schema) the user's own free-form keys form the contract."""
    extra_fields = extra_fields or {}
    if topology == CUSTOM_TOPOLOGY_VALUE:
        return dict(extra_fields)
    return {f["name"]: extra_fields.get(f["name"]) for f in topology_fields(topology)}


def compute_soft_warnings(topology: str, extra_fields: dict | None) -> list[str]:
    """Evaluate the schema's soft warn_above/warn_below feasibility thresholds
    against a submitted spec and return the active warning strings (empty list
    when none). Same logic the form applies live (SpecForm.extraFieldWarning),
    duplicated server-side so the warnings that were active at submit time can
    be persisted into the run record - a result viewed later then still
    carries its "this spec was aggressive for sky130" context, and API users
    (who never see the form) get the same flags. Non-blocking by design;
    hard min/max bounds live in validate_extra_fields instead."""
    extra_fields = extra_fields or {}
    warnings: list[str] = []
    for f in topology_fields(topology):
        value = extra_fields.get(f["name"])
        if value is None or value == "" or f.get("type") == "text":
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        unit = f.get("unit") or ""
        unit_sfx = f" {unit}" if unit else ""
        if f.get("warn_above") is not None and num > f["warn_above"] and f.get("warn_text"):
            warnings.append(f"{f['label']} = {value}{unit_sfx}: {f['warn_text']}")
        elif f.get("warn_below") is not None and num < f["warn_below"] and f.get("warn_text"):
            warnings.append(f"{f['label']} = {value}{unit_sfx}: {f['warn_text']}")
    return warnings
