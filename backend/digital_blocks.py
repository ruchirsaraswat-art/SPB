"""
Digital block catalog for the digital-architecture workspace (feature spec:
audits/2026-08-23-digital-arch-spec.md, section a).

Served by GET /api/digital_blocks in the EXACT same shape as /api/topologies
(groups -> options -> "fields" + "models" injected), but digital block values
are a SEPARATE namespace from the analog topologies: they are deliberately
NOT added to topologies.TOPOLOGY_GROUPS and are never handed to
Circuit_Builder's analog flow. Validation of a digital diagram's `topology`
strings (arch_chat view="digital") happens against ALL_DIGITAL_BLOCK_VALUES.

Field schema style mirrors topologies.TOPOLOGY_FIELDS exactly
(name/label/unit/help, optional min/max/warn_above/warn_text, type:"text").

Model applicability (models_contract-style {type: True | "<reason>"}):
  - veriloga: disabled for every digital block (synchronous logic has no
    compact-model behavior);
  - verilog: True for all;
  - rnm: True only for blocks that drive/observe real-valued AFE quantities
    in a mixed simulation (cal/training/monitor blocks), else disabled.
These are forward hooks only - v1 does NOT generate RTL for digital blocks
(spec section e); wiring them into the Circuit_Builder run flow is a future
spec of its own.
"""

from __future__ import annotations

_VERILOGA_SKIP = (
    "Verilog-A skipped: this is a synchronous digital block - it has no "
    "compact-model behavior."
)
_RNM_SKIP = (
    "RNM skipped: pure bit-level block - the digital Verilog model already is "
    "the behavioral model; real-valued ports add nothing."
)

# Blocks that drive/observe real-valued AFE quantities in a mixed simulation.
_RNM_BLOCKS = {
    "link_training_fsm",
    "power_state_fsm",
    "cal_engine",
    "eye_monitor_ctrl",
    "ddr_training_engine",
    "zq_cal_fsm",
    "d2d_link_state_fsm",
}

# sky130 synthesized-logic soft ceiling - same warning style/text family as
# the analog catalog's sky130 feasibility warnings.
_SKY130_LOGIC_WARN = "Above ~200 MHz is aggressive for sky130 synthesized logic."

_DFI_RATIO_FIELD = {
    "name": "dfi_ratio", "label": "DFI frequency ratio", "unit": "", "type": "text",
    "help": "'1:1', '1:2' or '1:4' - memory-clock to dfi_clk ratio; sets how many "
    "command slots per dfi_clk.",
}

DIGITAL_BLOCK_GROUPS: list[dict] = [
    {
        "group": "Alignment & elasticity",
        "options": [
            {
                "value": "word_aligner",
                "label": "Word / comma aligner (bit-slip)",
                "fields": [
                    {"name": "parallel_width_bits", "label": "Parallel word width", "unit": "bits", "min": 1,
                     "help": "Width of the deserializer word being aligned (e.g. 16 or 20). Must match the AFE deserializer's parallel width."},
                    {"name": "align_pattern", "label": "Alignment pattern", "unit": "", "type": "text",
                     "help": "The pattern searched for, e.g. K28.5 comma (8b/10b), 0x1E training pattern, or a PRBS sync word."},
                    {"name": "slip_granularity_bits", "label": "Bit-slip granularity", "unit": "bits", "min": 1,
                     "help": "How many bit positions one slip step moves; 1 for a barrel-shifter aligner."},
                    {"name": "lock_threshold_patterns", "label": "Lock threshold", "unit": "patterns", "min": 1,
                     "help": "Consecutive good patterns before declaring word lock; higher = slower lock, fewer false locks."},
                    {"name": "align_latency_cycles", "label": "Alignment latency", "unit": "cycles", "min": 0,
                     "help": "Word-clock cycles of pipeline delay through the aligner once locked."},
                ],
            },
            {
                "value": "lane_deskew",
                "label": "Multi-lane deskew aligner",
                "fields": [
                    {"name": "num_lanes", "label": "Number of lanes", "unit": "", "min": 2},
                    {"name": "deskew_range_ui", "label": "Deskew range", "unit": "UI", "min": 1,
                     "help": "Maximum lane-to-lane skew correctable, in unit intervals of the line rate."},
                    {"name": "deskew_marker", "label": "Deskew marker pattern", "unit": "", "type": "text",
                     "help": "Per-lane marker used to measure skew (e.g. UCIe-style valid framing, or a training header)."},
                    {"name": "fifo_depth_words", "label": "Per-lane FIFO depth", "unit": "words", "min": 2},
                ],
            },
            {
                "value": "elastic_fifo",
                "label": "Elastic / clock-compensation FIFO",
                "fields": [
                    {"name": "depth_words", "label": "FIFO depth", "unit": "words", "min": 4,
                     "help": "Total depth; must absorb ppm drift over the longest un-compensated interval plus skid margin."},
                    {"name": "width_bits", "label": "Data width", "unit": "bits", "min": 1},
                    {"name": "ppm_tolerance", "label": "Frequency offset tolerance", "unit": "ppm", "min": 0,
                     "warn_above": 1000,
                     "warn_text": "Above 1000 ppm is outside normal plesiochronous operation - check the clocking plan.",
                     "help": "Worst-case reader/writer frequency offset the FIFO must absorb (e.g. +/-300 ppm for separate reference crystals)."},
                    {"name": "skip_symbol", "label": "Skip/idle symbol", "unit": "", "type": "text",
                     "help": "Symbol inserted/deleted for rate compensation (e.g. K28.0 SKP); blank if the FIFO only absorbs phase, not frequency."},
                ],
            },
            {
                "value": "gearbox",
                "label": "Width gearbox (N:M rate converter)",
                "fields": [
                    {"name": "input_width_bits", "label": "Input width", "unit": "bits", "min": 1},
                    {"name": "output_width_bits", "label": "Output width", "unit": "bits", "min": 1},
                    {"name": "output_clock_mhz", "label": "Output-side clock", "unit": "MHz", "min": 0.001,
                     "warn_above": 200, "warn_text": _SKY130_LOGIC_WARN},
                ],
            },
        ],
    },
    {
        "group": "Line coding & integrity",
        "options": [
            {
                "value": "line_codec_8b10b",
                "label": "8b/10b encoder/decoder",
                "fields": [
                    {"name": "num_byte_lanes", "label": "Byte lanes", "unit": "", "min": 1,
                     "help": "How many 8b/10b symbols are coded per word clock (e.g. 2 for a 16-bit/20-bit path)."},
                    {"name": "disparity_error_action", "label": "Disparity/code error action", "unit": "", "type": "text",
                     "help": "What a code violation does: flag-only, substitute error symbol (/E/), or force retrain."},
                    {"name": "latency_cycles", "label": "Codec latency", "unit": "cycles", "min": 0},
                ],
            },
            {
                "value": "scrambler_descrambler",
                "label": "Scrambler / descrambler",
                "fields": [
                    {"name": "polynomial", "label": "LFSR polynomial", "unit": "", "type": "text",
                     "help": "e.g. x^58+x^39+1 (64b/66b, self-synchronizing) or x^16+x^5+x^4+x^3+1 (PCIe additive)."},
                    {"name": "mode", "label": "Mode", "unit": "", "type": "text",
                     "help": "'self-sync' (no seed exchange, error multiplication) or 'additive' (seeded, needs sync)."},
                    {"name": "width_bits", "label": "Datapath width", "unit": "bits", "min": 1},
                ],
            },
            {
                "value": "crc_retry_engine",
                "label": "CRC + retry engine (link-level ARQ)",
                "fields": [
                    {"name": "crc_polynomial", "label": "CRC polynomial", "unit": "", "type": "text",
                     "help": "e.g. CRC-16 (UCIe-style per-flit) or CRC-32."},
                    {"name": "flit_size_bits", "label": "Protected unit (flit) size", "unit": "bits", "min": 8},
                    {"name": "retry_buffer_flits", "label": "Retry buffer depth", "unit": "flits", "min": 1,
                     "help": "Must cover the round-trip latency at full rate: depth >= RTT_cycles * flits_per_cycle."},
                ],
            },
            {
                "value": "prbs_gen_checker",
                "label": "PRBS generator / checker (BIST)",
                "fields": [
                    {"name": "prbs_order", "label": "PRBS order", "unit": "", "min": 7, "max": 31,
                     "help": "7/9/15/23/31 - PRBS7 for quick bring-up, PRBS31 for stress; order = LFSR length."},
                    {"name": "error_counter_bits", "label": "Error counter width", "unit": "bits", "min": 8},
                    {"name": "parallel_width_bits", "label": "Parallel width", "unit": "bits", "min": 1,
                     "help": "The checker runs word-parallel at the word clock - width must match the datapath."},
                ],
            },
        ],
    },
    {
        "group": "Control & status",
        "options": [
            {
                "value": "csr_regfile",
                "label": "CSR / register file",
                "fields": [
                    {"name": "bus_protocol", "label": "Host bus protocol", "unit": "", "type": "text",
                     "help": "APB3 recommended for v1 (2-cycle, no burst) - AXI4-Lite or custom also fine; this is what the SoC uses to reach every knob in this catalog."},
                    {"name": "addr_width_bits", "label": "Address width", "unit": "bits", "min": 4, "max": 32},
                    {"name": "data_width_bits", "label": "Data width", "unit": "bits", "min": 8, "max": 64},
                    {"name": "num_registers", "label": "Register count (estimate)", "unit": "", "min": 1,
                     "help": "Drives the auto-generated register map size; every AFE trim/enable/status signal in the interface table lands here."},
                    {"name": "clock_freq_mhz", "label": "CSR clock", "unit": "MHz", "min": 0.001,
                     "warn_above": 200, "warn_text": _SKY130_LOGIC_WARN},
                ],
            },
            {
                "value": "link_training_fsm",
                "label": "Link training / adaptation FSM",
                "fields": [
                    {"name": "num_states", "label": "State count (estimate)", "unit": "", "min": 2,
                     "help": "e.g. RESET -> WAIT_PLL -> WAIT_CDR -> ALIGN -> ADAPT -> ACTIVE -> ERROR."},
                    {"name": "adapt_targets", "label": "Adaptation targets", "unit": "", "type": "text",
                     "help": "Which AFE knobs it tunes, e.g. 'CTLE peak, VGA gain, DFE tap1, slicer offset' - each must be a d2a signal in the interface table."},
                    {"name": "training_timeout_ms", "label": "Training timeout", "unit": "ms", "min": 0,
                     "help": "Give-up time before ERROR state; sets the watchdog counter size."},
                    {"name": "adapt_algorithm", "label": "Adaptation algorithm", "unit": "", "type": "text",
                     "help": "e.g. 'eye-height hill climb via eye monitor', 'sign-sign LMS on DFE error slicer'."},
                ],
            },
            {
                "value": "power_state_fsm",
                "label": "Power/reset sequencing FSM",
                "fields": [
                    {"name": "num_power_states", "label": "Power state count", "unit": "", "min": 2,
                     "help": "e.g. OFF / SLEEP (bias on) / STANDBY (PLL on) / ACTIVE."},
                    {"name": "sequence_step_time_us", "label": "Per-step settle time", "unit": "us", "min": 0,
                     "help": "Default wait between enable steps when no status signal gates the step."},
                    {"name": "wake_latency_target_us", "label": "Wake latency target", "unit": "us", "min": 0},
                ],
            },
            {
                "value": "cal_engine",
                "label": "Calibration engine (offset/impedance/Vref trim)",
                "fields": [
                    {"name": "num_trim_dacs", "label": "Trim controls driven", "unit": "", "min": 1,
                     "help": "How many AFE trim buses this engine sweeps (each is a d2a bus in the interface table)."},
                    {"name": "trim_resolution_bits", "label": "Trim resolution", "unit": "bits", "min": 1, "max": 12},
                    {"name": "cal_time_budget_us", "label": "Calibration time budget", "unit": "us", "min": 0},
                    {"name": "algorithm", "label": "Search algorithm", "unit": "", "type": "text",
                     "help": "e.g. 'binary search on comparator output', 'linear sweep + midpoint'."},
                ],
            },
            {
                "value": "eye_monitor_ctrl",
                "label": "Eye monitor controller",
                "fields": [
                    {"name": "phase_steps", "label": "Phase steps across UI", "unit": "", "min": 2,
                     "help": "Horizontal eye resolution - matches the AFE phase-interpolator step count."},
                    {"name": "vref_steps", "label": "Vref steps", "unit": "", "min": 2,
                     "help": "Vertical eye resolution - matches the error-slicer threshold DAC."},
                    {"name": "ber_counter_bits", "label": "Sample counter width", "unit": "bits", "min": 8,
                     "help": "Samples per eye point; 2^N samples bounds the measurable BER floor."},
                ],
            },
        ],
    },
    {
        "group": "Protocol / framing",
        "options": [
            {
                "value": "protocol_engine",
                "label": "Protocol engine / PCS framing",
                "fields": [
                    {"name": "standard", "label": "Framing standard", "unit": "", "type": "text",
                     "help": "e.g. '802.3-like PCS', 'custom packet', 'raw word stream' - v1 treats this as documentation, not behavior."},
                    {"name": "word_width_bits", "label": "Word width", "unit": "bits", "min": 1},
                    {"name": "flow_control", "label": "Flow control toward core", "unit": "", "type": "text",
                     "help": "'valid only', 'valid/ready', or 'credit' - must match the core-side signals in the interface table."},
                ],
            },
        ],
    },
    {
        # The digital side of a memory PHY presents a DFI-shaped interface to
        # the memory controller (DFI 5.x conventions: command + write + read
        # channels, ratio'd clocks, tphy_wrlat/trddata_en timing contracts).
        "group": "Memory interface (DFI-style, LPDDR/DDR/HBM)",
        "options": [
            {
                "value": "dfi_command_path",
                "label": "DFI command/address path",
                "fields": [
                    dict(_DFI_RATIO_FIELD),
                    {"name": "dfi_clk_mhz", "label": "dfi_clk frequency", "unit": "MHz", "min": 0.001,
                     "warn_above": 200, "warn_text": _SKY130_LOGIC_WARN},
                    {"name": "ca_width_bits", "label": "CA bus width", "unit": "bits", "min": 1,
                     "help": "Command/address width per slot (e.g. 7 for LPDDR4 CA)."},
                ],
            },
            {
                "value": "dfi_write_path",
                "label": "DFI write datapath (gearbox + phase align)",
                "fields": [
                    dict(_DFI_RATIO_FIELD),
                    {"name": "dq_width_bits", "label": "DQ lane width", "unit": "bits", "min": 1},
                    {"name": "tphy_wrlat_cycles", "label": "tphy_wrlat", "unit": "dfi_clk cycles", "min": 0,
                     "help": "DFI contract: delay from write command to wrdata_en the PHY requires - a spec output of this block, consumed by the MC."},
                    {"name": "phase_resolution_steps", "label": "TX phase resolution", "unit": "steps", "min": 1,
                     "help": "Steps of the AFE DLL/PI this path can request for write-leveling."},
                ],
            },
            {
                "value": "dfi_read_path",
                "label": "DFI read datapath (capture FIFO + gearbox)",
                "fields": [
                    dict(_DFI_RATIO_FIELD),
                    {"name": "dq_width_bits", "label": "DQ lane width", "unit": "bits", "min": 1},
                    {"name": "trddata_en_cycles", "label": "trddata_en", "unit": "dfi_clk cycles", "min": 0,
                     "help": "DFI contract: read command to rddata_en delay."},
                    {"name": "capture_fifo_depth", "label": "Read-capture FIFO depth", "unit": "words", "min": 2,
                     "help": "Absorbs DQS-domain to dfi_clk-domain uncertainty (round-trip flight time variation)."},
                ],
            },
            {
                "value": "ddr_training_engine",
                "label": "DDR training engine (leveling/deskew/Vref)",
                "fields": [
                    {"name": "trainings", "label": "Training steps supported", "unit": "", "type": "text",
                     "help": "e.g. 'write leveling, read gate, per-bit deskew, Vref(DQ)' - each maps to d2a controls in the interface table."},
                    {"name": "deskew_range_ps", "label": "Per-bit deskew range", "unit": "ps", "min": 0},
                    {"name": "deskew_step_ps", "label": "Deskew step", "unit": "ps", "min": 0,
                     "help": "Must match the AFE DLL/PI phase_step_ps."},
                    {"name": "pattern_source", "label": "Training pattern source", "unit": "", "type": "text",
                     "help": "'MC-driven (DFI 5.x style)' or 'PHY-local pattern generator'."},
                ],
            },
            {
                "value": "zq_cal_fsm",
                "label": "ZQ / impedance calibration FSM",
                "fields": [
                    {"name": "cal_interval_ms", "label": "Recalibration interval", "unit": "ms", "min": 0,
                     "help": "0 = only at init; periodic recal tracks voltage/temperature drift of the driver impedance."},
                    {"name": "code_width_bits", "label": "Impedance code width", "unit": "bits", "min": 1, "max": 8,
                     "help": "Width of the pull-up/pull-down strength code driven to the AFE driver."},
                    {"name": "settle_cycles_per_step", "label": "Comparator settle time", "unit": "cycles", "min": 1},
                ],
            },
        ],
    },
    {
        "group": "Die-to-die (UCIe-style)",
        "options": [
            {
                # Low-speed always-on serial channel for parameter exchange /
                # training messages: own clock, alive before mainband.
                "value": "d2d_sideband_ctrl",
                "label": "Sideband controller",
                "fields": [
                    {"name": "sideband_rate_mbps", "label": "Sideband rate", "unit": "Mb/s", "min": 0.001,
                     "warn_above": 100,
                     "warn_text": "Keep the sideband slow and simple - it must work before any training."},
                    {"name": "packet_format", "label": "Packet format", "unit": "", "type": "text",
                     "help": "e.g. '32-bit header + 32/64-bit data, UCIe-like' - documentation in v1."},
                ],
            },
            {
                "value": "d2d_link_state_fsm",
                "label": "Link state machine (LTSM)",
                "fields": [
                    {"name": "num_states", "label": "State count (estimate)", "unit": "", "min": 2,
                     "help": "e.g. RESET -> SBINIT -> PARAM -> MBINIT (cal+repair) -> MBTRAIN -> ACTIVE, UCIe-style."},
                    {"name": "timeout_ms", "label": "Per-state timeout", "unit": "ms", "min": 0},
                ],
            },
            {
                "value": "lane_repair_mux",
                "label": "Lane repair / remap mux",
                "fields": [
                    {"name": "num_lanes", "label": "Active lanes", "unit": "", "min": 1},
                    {"name": "num_spare_lanes", "label": "Spare lanes", "unit": "", "min": 0},
                    {"name": "repair_granularity", "label": "Repair granularity", "unit": "", "type": "text",
                     "help": "'single lane shift' (UCIe-like) or 'arbitrary remap'."},
                ],
            },
        ],
    },
]

# Inject the model-applicability entry per option, mirroring topologies.py's
# injection so GET /api/digital_blocks already carries everything the digital
# view needs (fields are authored inline above).
for _group in DIGITAL_BLOCK_GROUPS:
    for _opt in _group["options"]:
        _opt["models"] = {
            "veriloga": _VERILOGA_SKIP,
            "verilog": True,
            "rnm": True if _opt["value"] in _RNM_BLOCKS else _RNM_SKIP,
        }

DIGITAL_BLOCK_LABELS: dict[str, str] = {
    opt["value"]: opt["label"] for group in DIGITAL_BLOCK_GROUPS for opt in group["options"]
}

ALL_DIGITAL_BLOCK_VALUES: set[str] = set(DIGITAL_BLOCK_LABELS)

# Which blocks the digital view offers per PHY type (same role the AFE
# diagrams' block set plays). The chat consultant validates add_block against
# this SOFTLY - a warning, not a rejection, since users may legitimately
# borrow blocks across PHY families.
_SERDES_BLOCKS = [
    "word_aligner", "elastic_fifo", "gearbox", "line_codec_8b10b",
    "scrambler_descrambler", "crc_retry_engine", "prbs_gen_checker",
    "csr_regfile", "link_training_fsm", "power_state_fsm", "cal_engine",
    "eye_monitor_ctrl", "protocol_engine",
]
_MEM_BLOCKS = [
    "dfi_command_path", "dfi_write_path", "dfi_read_path",
    "ddr_training_engine", "zq_cal_fsm", "csr_regfile", "power_state_fsm",
    "prbs_gen_checker",
]

DIGITAL_BLOCKS_BY_PHY: dict[str, list[str]] = {
    "ser-des": list(_SERDES_BLOCKS),
    "lpddr": list(_MEM_BLOCKS),
    "ddr": list(_MEM_BLOCKS),
    "hbm": list(_MEM_BLOCKS) + ["lane_repair_mux"],
    "die-to-die": [
        "lane_deskew", "gearbox", "elastic_fifo", "scrambler_descrambler",
        "crc_retry_engine", "d2d_sideband_ctrl", "d2d_link_state_fsm",
        "lane_repair_mux", "prbs_gen_checker", "csr_regfile", "power_state_fsm",
    ],
    "optical": [
        "word_aligner", "elastic_fifo", "line_codec_8b10b",
        "scrambler_descrambler", "prbs_gen_checker", "csr_regfile",
        "link_training_fsm", "power_state_fsm", "cal_engine",
        "eye_monitor_ctrl", "protocol_engine",
    ],
}
