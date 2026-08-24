# 2026-08-23 - Digital architecture + AFE<->digital interface workspace: feature spec

Author: Digital_Microarchitect (digital-interface / controller microarchitecture
research agent). This is the implementable feature spec for extending
analog-spec-tool beyond the PHY/AFE: a digital block catalog, digital
architecture diagrams, and - the core deliverable - an AFE<->digital interface
workspace (signal-table editor with chat-consultant integration).

Research notes + sources: `~/.claude/agent-knowledge/digital-microarch/`
(INDEX.md -> phy-digital-block-catalog.md, afe-digital-interface-patterns.md).

Conventions this spec deliberately mirrors (do not invent parallel ones):

- Field schema style = `backend/topologies.py` `TOPOLOGY_FIELDS`
  (`name/label/unit/help`, optional `min/max/warn_*`, `type:"text"`).
- Diagram data shape = `frontend/src/phyArchitectures.js` `PHY_ARCHITECTURES`
  (`blocks:[{id,label,short,topology,col,row,kind?}]`, `edges:[{from,to}]`).
- Chat/patch contract = `audits/2026-08-21-arch-chat-feature.md`
  (session_id rules, sync POST, `patch.ops` applied in order, server-side
  validation, 422/502 semantics, localStorage snapshot/undo).
- Model levels = `backend/models_contract.py` (`veriloga`/`verilog`/`rnm`;
  digital Verilog + RNM verified with `iverilog -g2012`; disabled levels carry
  a human-readable reason string).
- sky130 feasibility: line rates ~1-2 Gb/s; digital word clocks 50-200 MHz
  (sky130 std-cell designs routinely close ~100 MHz). Same
  `warn_above` soft-warning style as the analog fields.

---

## (a) Digital block catalog

New file `backend/digital_blocks.py`, served by `GET /api/digital_blocks`,
exact same shape as `/api/topologies` (groups -> options -> `fields` +
`models` injected). Digital block values are a SEPARATE namespace from analog
topologies - they must NOT be added to `TOPOLOGY_GROUPS` (they are never
handed to Circuit_Builder's analog flow). Validation of a digital diagram's
`topology` strings happens against this catalog (see view-aware chat, below).

Model applicability for every digital block: `veriloga` disabled with reason
`"Verilog-A skipped: this is a synchronous digital block - it has no
compact-model behavior."`; `verilog: True` for all; `rnm: True` only for
blocks that drive/observe real-valued AFE quantities in a mixed simulation
(cal/training/monitor blocks - marked RNM below), otherwise disabled with
reason `"RNM skipped: pure bit-level block - the digital Verilog model
already is the behavioral model; real-valued ports add nothing."`

### Catalog (value / label / fields / RNM? )

Group **"Alignment & elasticity"** (SerDes, optical, D2D):

- `word_aligner` - "Word / comma aligner (bit-slip)"
  - `parallel_width_bits` ("Parallel word width", "bits", min 1) - help: "Width of the deserializer word being aligned (e.g. 16 or 20). Must match the AFE deserializer's parallel width."
  - `align_pattern` ("Alignment pattern", "", text) - help: "The pattern searched for, e.g. K28.5 comma (8b/10b), 0x1E training pattern, or a PRBS sync word."
  - `slip_granularity_bits` ("Bit-slip granularity", "bits", min 1) - help: "How many bit positions one slip step moves; 1 for a barrel-shifter aligner."
  - `lock_threshold_patterns` ("Lock threshold", "patterns", min 1) - help: "Consecutive good patterns before declaring word lock; higher = slower lock, fewer false locks."
  - `align_latency_cycles` ("Alignment latency", "cycles", min 0) - help: "Word-clock cycles of pipeline delay through the aligner once locked."
- `lane_deskew` - "Multi-lane deskew aligner" (D2D, multi-lane SerDes)
  - `num_lanes` ("Number of lanes", "", min 2)
  - `deskew_range_ui` ("Deskew range", "UI", min 1) - help: "Maximum lane-to-lane skew correctable, in unit intervals of the line rate."
  - `deskew_marker` ("Deskew marker pattern", "", text) - help: "Per-lane marker used to measure skew (e.g. UCIe-style valid framing, or a training header)."
  - `fifo_depth_words` ("Per-lane FIFO depth", "words", min 2)
- `elastic_fifo` - "Elastic / clock-compensation FIFO"
  - `depth_words` ("FIFO depth", "words", min 4) - help: "Total depth; must absorb ppm drift over the longest un-compensated interval plus skid margin."
  - `width_bits` ("Data width", "bits", min 1)
  - `ppm_tolerance` ("Frequency offset tolerance", "ppm", min 0, warn_above 1000, warn_text "Above 1000 ppm is outside normal plesiochronous operation - check the clocking plan.") - help: "Worst-case reader/writer frequency offset the FIFO must absorb (e.g. +/-300 ppm for separate reference crystals)."
  - `skip_symbol` ("Skip/idle symbol", "", text) - help: "Symbol inserted/deleted for rate compensation (e.g. K28.0 SKP); blank if the FIFO only absorbs phase, not frequency."
- `gearbox` - "Width gearbox (N:M rate converter)" (used for 64b/66b, DFI ratios, D2D mainband)
  - `input_width_bits` ("Input width", "bits", min 1)
  - `output_width_bits` ("Output width", "bits", min 1)
  - `output_clock_mhz` ("Output-side clock", "MHz", min 0.001, warn_above 200, warn_text "Above ~200 MHz is aggressive for sky130 synthesized logic.")

Group **"Line coding & integrity"**:

- `line_codec_8b10b` - "8b/10b encoder/decoder"
  - `num_byte_lanes` ("Byte lanes", "", min 1) - help: "How many 8b/10b symbols are coded per word clock (e.g. 2 for a 16-bit/20-bit path)."
  - `disparity_error_action` ("Disparity/code error action", "", text) - help: "What a code violation does: flag-only, substitute error symbol (/E/), or force retrain."
  - `latency_cycles` ("Codec latency", "cycles", min 0)
- `scrambler_descrambler` - "Scrambler / descrambler"
  - `polynomial` ("LFSR polynomial", "", text) - help: "e.g. x^58+x^39+1 (64b/66b, self-synchronizing) or x^16+x^5+x^4+x^3+1 (PCIe additive)."
  - `mode` ("Mode", "", text) - help: "'self-sync' (no seed exchange, error multiplication) or 'additive' (seeded, needs sync)."
  - `width_bits` ("Datapath width", "bits", min 1)
- `crc_retry_engine` - "CRC + retry engine (link-level ARQ)" (D2D adapter, optional SerDes)
  - `crc_polynomial` ("CRC polynomial", "", text) - help: "e.g. CRC-16 (UCIe-style per-flit) or CRC-32."
  - `flit_size_bits` ("Protected unit (flit) size", "bits", min 8)
  - `retry_buffer_flits` ("Retry buffer depth", "flits", min 1) - help: "Must cover the round-trip latency at full rate: depth >= RTT_cycles * flits_per_cycle."
- `prbs_gen_checker` - "PRBS generator / checker (BIST)"
  - `prbs_order` ("PRBS order", "", min 7, max 31) - help: "7/9/15/23/31 - PRBS7 for quick bring-up, PRBS31 for stress; order = LFSR length."
  - `error_counter_bits` ("Error counter width", "bits", min 8)
  - `parallel_width_bits` ("Parallel width", "bits", min 1) - help: "The checker runs word-parallel at the word clock - width must match the datapath."

Group **"Control & status"**:

- `csr_regfile` - "CSR / register file" 
  - `bus_protocol` ("Host bus protocol", "", text) - help: "APB3 recommended for v1 (2-cycle, no burst) - AXI4-Lite or custom also fine; this is what the SoC uses to reach every knob in this catalog."
  - `addr_width_bits` ("Address width", "bits", min 4, max 32)
  - `data_width_bits` ("Data width", "bits", min 8, max 64)
  - `num_registers` ("Register count (estimate)", "", min 1) - help: "Drives the auto-generated register map size; every AFE trim/enable/status signal in the interface table lands here."
  - `clock_freq_mhz` ("CSR clock", "MHz", min 0.001, warn_above 200, warn_text "Above ~200 MHz is aggressive for sky130 synthesized logic.")
- `link_training_fsm` - "Link training / adaptation FSM" **[RNM: True]**
  - `num_states` ("State count (estimate)", "", min 2) - help: "e.g. RESET -> WAIT_PLL -> WAIT_CDR -> ALIGN -> ADAPT -> ACTIVE -> ERROR."
  - `adapt_targets` ("Adaptation targets", "", text) - help: "Which AFE knobs it tunes, e.g. 'CTLE peak, VGA gain, DFE tap1, slicer offset' - each must be a d2a signal in the interface table."
  - `training_timeout_ms` ("Training timeout", "ms", min 0) - help: "Give-up time before ERROR state; sets the watchdog counter size."
  - `adapt_algorithm` ("Adaptation algorithm", "", text) - help: "e.g. 'eye-height hill climb via eye monitor', 'sign-sign LMS on DFE error slicer'."
- `power_state_fsm` - "Power/reset sequencing FSM" **[RNM: True]**
  - `num_power_states` ("Power state count", "", min 2) - help: "e.g. OFF / SLEEP (bias on) / STANDBY (PLL on) / ACTIVE."
  - `sequence_step_time_us` ("Per-step settle time", "us", min 0) - help: "Default wait between enable steps when no status signal gates the step."
  - `wake_latency_target_us` ("Wake latency target", "us", min 0)
- `cal_engine` - "Calibration engine (offset/impedance/Vref trim)" **[RNM: True]**
  - `num_trim_dacs` ("Trim controls driven", "", min 1) - help: "How many AFE trim buses this engine sweeps (each is a d2a bus in the interface table)."
  - `trim_resolution_bits` ("Trim resolution", "bits", min 1, max 12)
  - `cal_time_budget_us` ("Calibration time budget", "us", min 0)
  - `algorithm` ("Search algorithm", "", text) - help: "e.g. 'binary search on comparator output', 'linear sweep + midpoint'."
- `eye_monitor_ctrl` - "Eye monitor controller" **[RNM: True]**
  - `phase_steps` ("Phase steps across UI", "", min 2) - help: "Horizontal eye resolution - matches the AFE phase-interpolator step count."
  - `vref_steps` ("Vref steps", "", min 2) - help: "Vertical eye resolution - matches the error-slicer threshold DAC."
  - `ber_counter_bits` ("Sample counter width", "bits", min 8) - help: "Samples per eye point; 2^N samples bounds the measurable BER floor."

Group **"Protocol / framing"**:

- `protocol_engine` - "Protocol engine / PCS framing"
  - `standard` ("Framing standard", "", text) - help: "e.g. '802.3-like PCS', 'custom packet', 'raw word stream' - v1 treats this as documentation, not behavior."
  - `word_width_bits` ("Word width", "bits", min 1)
  - `flow_control` ("Flow control toward core", "", text) - help: "'valid only', 'valid/ready', or 'credit' - must match the core-side signals in the interface table."

Group **"Memory interface (DFI-style, LPDDR/DDR/HBM)"** - the digital side
of a memory PHY presents a DFI-shaped interface to the memory controller
(DFI 5.x conventions: command + write + read channels, ratio'd clocks,
tphy_wrlat/trddata_en timing contracts):

- `dfi_command_path` - "DFI command/address path"
  - `dfi_ratio` ("DFI frequency ratio", "", text) - help: "'1:1', '1:2' or '1:4' - memory-clock to dfi_clk ratio; sets how many command slots per dfi_clk."
  - `dfi_clk_mhz` ("dfi_clk frequency", "MHz", min 0.001, warn_above 200, warn_text "Above ~200 MHz is aggressive for sky130 synthesized logic.")
  - `ca_width_bits` ("CA bus width", "bits", min 1) - help: "Command/address width per slot (e.g. 7 for LPDDR4 CA)."
- `dfi_write_path` - "DFI write datapath (gearbox + phase align)"
  - `dfi_ratio` (as above)
  - `dq_width_bits` ("DQ lane width", "bits", min 1)
  - `tphy_wrlat_cycles` ("tphy_wrlat", "dfi_clk cycles", min 0) - help: "DFI contract: delay from write command to wrdata_en the PHY requires - a spec output of this block, consumed by the MC."
  - `phase_resolution_steps` ("TX phase resolution", "steps", min 1) - help: "Steps of the AFE DLL/PI this path can request for write-leveling."
- `dfi_read_path` - "DFI read datapath (capture FIFO + gearbox)"
  - `dfi_ratio` (as above)
  - `dq_width_bits` (as above)
  - `trddata_en_cycles` ("trddata_en", "dfi_clk cycles", min 0) - help: "DFI contract: read command to rddata_en delay."
  - `capture_fifo_depth` ("Read-capture FIFO depth", "words", min 2) - help: "Absorbs DQS-domain to dfi_clk-domain uncertainty (round-trip flight time variation)."
- `ddr_training_engine` - "DDR training engine (leveling/deskew/Vref)" **[RNM: True]**
  - `trainings` ("Training steps supported", "", text) - help: "e.g. 'write leveling, read gate, per-bit deskew, Vref(DQ)' - each maps to d2a controls in the interface table."
  - `deskew_range_ps` ("Per-bit deskew range", "ps", min 0)
  - `deskew_step_ps` ("Deskew step", "ps", min 0) - help: "Must match the AFE DLL/PI phase_step_ps."
  - `pattern_source` ("Training pattern source", "", text) - help: "'MC-driven (DFI 5.x style)' or 'PHY-local pattern generator'."
- `zq_cal_fsm` - "ZQ / impedance calibration FSM" **[RNM: True]**
  - `cal_interval_ms` ("Recalibration interval", "ms", min 0) - help: "0 = only at init; periodic recal tracks voltage/temperature drift of the driver impedance."
  - `code_width_bits` ("Impedance code width", "bits", min 1, max 8) - help: "Width of the pull-up/pull-down strength code driven to the AFE driver."
  - `settle_cycles_per_step` ("Comparator settle time", "cycles", min 1)

Group **"Die-to-die (UCIe-style)"**:

- `d2d_sideband_ctrl` - "Sideband controller" - help concept: low-speed always-on serial channel for parameter exchange/training messages, own clock, alive before mainband.
  - `sideband_rate_mbps` ("Sideband rate", "Mb/s", min 0.001, warn_above 100, warn_text "Keep the sideband slow and simple - it must work before any training.")
  - `packet_format` ("Packet format", "", text) - help: "e.g. '32-bit header + 32/64-bit data, UCIe-like' - documentation in v1."
- `d2d_link_state_fsm` - "Link state machine (LTSM)" **[RNM: True]**
  - `num_states` ("State count (estimate)", "", min 2) - help: "e.g. RESET -> SBINIT -> PARAM -> MBINIT (cal+repair) -> MBTRAIN -> ACTIVE, UCIe-style."
  - `timeout_ms` ("Per-state timeout", "ms", min 0)
- `lane_repair_mux` - "Lane repair / remap mux"
  - `num_lanes` ("Active lanes", "", min 1)
  - `num_spare_lanes` ("Spare lanes", "", min 0)
  - `repair_granularity` ("Repair granularity", "", text) - help: "'single lane shift' (UCIe-like) or 'arbitrary remap'."

### Per-PHY applicability map

`DIGITAL_BLOCKS_BY_PHY` (backend, exported with the catalog) - which blocks
the digital view offers per PHY type (same role the AFE diagrams' block set
plays; the chat consultant also validates add_block against it, softly - a
warning, not a rejection, since users may legitimately borrow blocks):

- `ser-des`: word_aligner, elastic_fifo, gearbox, line_codec_8b10b, scrambler_descrambler, crc_retry_engine, prbs_gen_checker, csr_regfile, link_training_fsm, power_state_fsm, cal_engine, eye_monitor_ctrl, protocol_engine
- `lpddr` / `ddr` / `hbm`: dfi_command_path, dfi_write_path, dfi_read_path, ddr_training_engine, zq_cal_fsm, csr_regfile, power_state_fsm, prbs_gen_checker (HBM adds lane_repair_mux)
- `die-to-die`: lane_deskew, gearbox, elastic_fifo, scrambler_descrambler, crc_retry_engine, d2d_sideband_ctrl, d2d_link_state_fsm, lane_repair_mux, prbs_gen_checker, csr_regfile, power_state_fsm
- `optical`: word_aligner, elastic_fifo, line_codec_8b10b, scrambler_descrambler, prbs_gen_checker, csr_regfile, link_training_fsm, power_state_fsm, cal_engine, eye_monitor_ctrl, protocol_engine

---

## (b) Digital architecture diagrams

New file `frontend/src/digitalArchitectures.js`, exporting
`DIGITAL_ARCHITECTURES` with EXACTLY the `PHY_ARCHITECTURES` data shape, and
reusing the same hierarchy-aware helper layer (factor the localStorage
helpers in `phyArchitectures.js` to take a key namespace - e.g. prefix
`dig-arch-` vs `arch-` - rather than duplicating them). `topology` values
reference the digital catalog; `topology: null` + `kind: "iface"` marks
boundary anchor nodes (AFE side, core side) - drawn, wireable, not
selectable, exactly like `kind: "pad"` in the AFE view. Rendering reuses
`ArchitectureDiagram` unchanged apart from the storage namespace and the
catalog used for the topology picker.

```js
export const DIGITAL_ARCHITECTURES = {
  "ser-des": {
    // Word-clock domain datapath: aligned -> decoded -> rate-matched -> framed.
    // Control row: training FSM owns the AFE knobs, CSR owns the host bus.
    blocks: [
      { id: "afe-rx-if", label: "AFE RX Data (from Deserializer)", short: "AFE RX IF", topology: null, kind: "iface", col: 0, row: 0 },
      { id: "rx-align", label: "Word Aligner (Comma Detect)", short: "Word Align", topology: "word_aligner", col: 1, row: 0 },
      { id: "rx-dec", label: "8b/10b Decoder", short: "8b10b Dec", topology: "line_codec_8b10b", col: 2, row: 0 },
      { id: "rx-fifo", label: "Elastic FIFO (CDC to Core Clock)", short: "Elastic FIFO", topology: "elastic_fifo", col: 3, row: 0 },
      { id: "rx-pcs", label: "Protocol Engine / Framer", short: "PCS", topology: "protocol_engine", col: 4, row: 0 },
      { id: "core-if", label: "Core / Fabric Interface", short: "CORE IF", topology: null, kind: "iface", col: 5, row: 0 },
      { id: "afe-ctl-if", label: "AFE Control & Status (Trim/Enable/Lock)", short: "AFE CTL IF", topology: null, kind: "iface", col: 0, row: 1 },
      { id: "prbs", label: "PRBS Checker (BIST)", short: "PRBS", topology: "prbs_gen_checker", col: 2, row: 1 },
      { id: "train", label: "Link Training & Adaptation FSM", short: "Train FSM", topology: "link_training_fsm", col: 3, row: 1 },
      { id: "pwr-seq", label: "Power/Reset Sequencer", short: "Pwr Seq", topology: "power_state_fsm", col: 1, row: 1 },
      { id: "csr", label: "CSR / Register File (APB)", short: "CSR", topology: "csr_regfile", col: 4, row: 1 },
      { id: "host-if", label: "Host Bus (APB)", short: "APB IF", topology: null, kind: "iface", col: 5, row: 1 },
      { id: "tx-enc", label: "8b/10b Encoder", short: "8b10b Enc", topology: "line_codec_8b10b", col: 3, row: 2 },
      { id: "tx-fifo", label: "TX FIFO (Core -> TX Word Clock)", short: "TX FIFO", topology: "elastic_fifo", col: 4, row: 2 },
      { id: "afe-tx-if", label: "AFE TX Data (to Serializer)", short: "AFE TX IF", topology: null, kind: "iface", col: 2, row: 2 },
    ],
    edges: [
      { from: "afe-rx-if", to: "rx-align" },
      { from: "rx-align", to: "rx-dec" },
      { from: "rx-dec", to: "rx-fifo" },
      { from: "rx-fifo", to: "rx-pcs" },
      { from: "rx-pcs", to: "core-if" },
      { from: "rx-align", to: "prbs" },
      { from: "prbs", to: "train" },
      { from: "train", to: "afe-ctl-if" },
      { from: "pwr-seq", to: "afe-ctl-if" },
      { from: "csr", to: "train" },
      { from: "csr", to: "pwr-seq" },
      { from: "host-if", to: "csr" },
      { from: "core-if", to: "tx-fifo" },
      { from: "tx-fifo", to: "tx-enc" },
      { from: "tx-enc", to: "afe-tx-if" },
    ],
  },
  "lpddr": {
    // DFI-shaped toward the memory controller: separate command, write and
    // read channels; training engine owns the AFE deskew/Vref knobs.
    blocks: [
      { id: "dfi-if", label: "DFI Interface (to Memory Controller)", short: "DFI IF", topology: null, kind: "iface", col: 0, row: 0 },
      { id: "cmd-path", label: "DFI Command/Address Path", short: "CMD Path", topology: "dfi_command_path", col: 1, row: 0 },
      { id: "afe-ca-if", label: "AFE CA Driver Interface", short: "AFE CA IF", topology: null, kind: "iface", col: 2, row: 0 },
      { id: "wr-path", label: "DFI Write Datapath (Gearbox)", short: "WR Path", topology: "dfi_write_path", col: 1, row: 1 },
      { id: "afe-dqtx-if", label: "AFE DQ TX Interface", short: "AFE DQTX IF", topology: null, kind: "iface", col: 2, row: 1 },
      { id: "rd-path", label: "DFI Read Datapath (Capture FIFO)", short: "RD Path", topology: "dfi_read_path", col: 1, row: 2 },
      { id: "afe-dqrx-if", label: "AFE DQ RX Interface (DQS domain)", short: "AFE DQRX IF", topology: null, kind: "iface", col: 2, row: 2 },
      { id: "train", label: "Training Engine (Leveling/Deskew/Vref)", short: "Training", topology: "ddr_training_engine", col: 2, row: 3 },
      { id: "zq", label: "ZQ Calibration FSM", short: "ZQ Cal", topology: "zq_cal_fsm", col: 3, row: 3 },
      { id: "afe-ctl-if", label: "AFE Control (DLL/PI codes, Vref, ZQ)", short: "AFE CTL IF", topology: null, kind: "iface", col: 4, row: 3 },
      { id: "csr", label: "CSR / Register File (APB)", short: "CSR", topology: "csr_regfile", col: 1, row: 3 },
      { id: "host-if", label: "Host Bus (APB)", short: "APB IF", topology: null, kind: "iface", col: 0, row: 3 },
      { id: "pwr-seq", label: "Init / Freq-switch / Power FSM", short: "Init FSM", topology: "power_state_fsm", col: 0, row: 2 },
    ],
    edges: [
      { from: "dfi-if", to: "cmd-path" }, { from: "cmd-path", to: "afe-ca-if" },
      { from: "dfi-if", to: "wr-path" }, { from: "wr-path", to: "afe-dqtx-if" },
      { from: "afe-dqrx-if", to: "rd-path" }, { from: "rd-path", to: "dfi-if" },
      { from: "host-if", to: "csr" }, { from: "csr", to: "train" },
      { from: "csr", to: "pwr-seq" }, { from: "train", to: "afe-ctl-if" },
      { from: "zq", to: "afe-ctl-if" }, { from: "pwr-seq", to: "afe-ctl-if" },
      { from: "train", to: "wr-path" }, { from: "train", to: "rd-path" },
    ],
  },
  "die-to-die": {
    // UCIe-flavored: mainband datapath with deskew + repair, sideband
    // controller with its own always-on channel, LTSM orchestrating both.
    blocks: [
      { id: "afe-rx-if", label: "AFE RX Data (per-lane, fwd-clock domain)", short: "AFE RX IF", topology: null, kind: "iface", col: 0, row: 0 },
      { id: "deskew", label: "Lane Deskew Aligner", short: "Deskew", topology: "lane_deskew", col: 1, row: 0 },
      { id: "rx-gbox", label: "RX Gearbox (to core width)", short: "RX Gearbox", topology: "gearbox", col: 2, row: 0 },
      { id: "crc", label: "CRC / Retry Engine", short: "CRC+Retry", topology: "crc_retry_engine", col: 3, row: 0 },
      { id: "rdi-if", label: "Adapter Interface (RDI-like)", short: "RDI IF", topology: null, kind: "iface", col: 4, row: 0 },
      { id: "tx-gbox", label: "TX Gearbox + Lane Map/Repair", short: "TX Gearbox", topology: "lane_repair_mux", col: 2, row: 1 },
      { id: "afe-tx-if", label: "AFE TX Data (per-lane)", short: "AFE TX IF", topology: null, kind: "iface", col: 1, row: 1 },
      { id: "ltsm", label: "Link State Machine (LTSM)", short: "LTSM", topology: "d2d_link_state_fsm", col: 3, row: 2 },
      { id: "sideband", label: "Sideband Controller (always-on)", short: "Sideband", topology: "d2d_sideband_ctrl", col: 2, row: 2 },
      { id: "sb-if", label: "Sideband Pins IF", short: "SB IF", topology: null, kind: "iface", col: 1, row: 2 },
      { id: "csr", label: "CSR / Register File (APB)", short: "CSR", topology: "csr_regfile", col: 4, row: 2 },
      { id: "afe-ctl-if", label: "AFE Control & Status", short: "AFE CTL IF", topology: null, kind: "iface", col: 0, row: 2 },
    ],
    edges: [
      { from: "afe-rx-if", to: "deskew" }, { from: "deskew", to: "rx-gbox" },
      { from: "rx-gbox", to: "crc" }, { from: "crc", to: "rdi-if" },
      { from: "rdi-if", to: "tx-gbox" }, { from: "tx-gbox", to: "afe-tx-if" },
      { from: "sb-if", to: "sideband" }, { from: "sideband", to: "ltsm" },
      { from: "ltsm", to: "afe-ctl-if" }, { from: "ltsm", to: "tx-gbox" },
      { from: "ltsm", to: "deskew" }, { from: "csr", to: "ltsm" },
    ],
  },
  "optical": {
    // SerDes-like word-clock datapath plus a calibration engine that owns
    // the TIA/LA analog trims and a loss-of-signal path into the link FSM.
    blocks: [
      { id: "afe-rx-if", label: "AFE RX Data (from Deserializer)", short: "AFE RX IF", topology: null, kind: "iface", col: 0, row: 0 },
      { id: "rx-align", label: "Word Aligner", short: "Word Align", topology: "word_aligner", col: 1, row: 0 },
      { id: "rx-dec", label: "8b/10b Decoder", short: "8b10b Dec", topology: "line_codec_8b10b", col: 2, row: 0 },
      { id: "rx-fifo", label: "Elastic FIFO", short: "Elastic FIFO", topology: "elastic_fifo", col: 3, row: 0 },
      { id: "core-if", label: "Core Interface", short: "CORE IF", topology: null, kind: "iface", col: 4, row: 0 },
      { id: "cal", label: "TIA/LA Calibration Engine (offset, gain)", short: "Cal Engine", topology: "cal_engine", col: 1, row: 1 },
      { id: "train", label: "Link FSM (incl. LOS handling)", short: "Link FSM", topology: "link_training_fsm", col: 2, row: 1 },
      { id: "afe-ctl-if", label: "AFE Control & Status (LOS, trims)", short: "AFE CTL IF", topology: null, kind: "iface", col: 0, row: 1 },
      { id: "csr", label: "CSR / Register File (APB)", short: "CSR", topology: "csr_regfile", col: 3, row: 1 },
      { id: "host-if", label: "Host Bus (APB)", short: "APB IF", topology: null, kind: "iface", col: 4, row: 1 },
      { id: "tx-enc", label: "8b/10b Encoder", short: "8b10b Enc", topology: "line_codec_8b10b", col: 2, row: 2 },
      { id: "afe-tx-if", label: "AFE TX Data (to Serializer/Mod Driver)", short: "AFE TX IF", topology: null, kind: "iface", col: 1, row: 2 },
    ],
    edges: [
      { from: "afe-rx-if", to: "rx-align" }, { from: "rx-align", to: "rx-dec" },
      { from: "rx-dec", to: "rx-fifo" }, { from: "rx-fifo", to: "core-if" },
      { from: "afe-ctl-if", to: "cal" }, { from: "cal", to: "afe-ctl-if" },
      { from: "cal", to: "train" }, { from: "train", to: "afe-ctl-if" },
      { from: "host-if", to: "csr" }, { from: "csr", to: "train" },
      { from: "csr", to: "cal" }, { from: "core-if", to: "tx-enc" },
      { from: "tx-enc", to: "afe-tx-if" },
    ],
  },
};
// "ddr" and "hbm": v1 reuses the "lpddr" digital architecture (DDR: identical
// structure; HBM: add lane_repair_mux between rd/wr paths and the AFE ifaces
// when someone asks). Map them in code: DIGITAL_ARCHITECTURES.ddr =
// DIGITAL_ARCHITECTURES.hbm = DIGITAL_ARCHITECTURES.lpddr (shallow reuse is
// fine - user edits are stored per phyType key, not on the shared object).
```

Chat on the digital view: reuse `POST /api/arch_chat` with one addition -
request gains optional `"view": "afe" | "digital"` (default `"afe"`).
For `"digital"` the server validates patch `topology` values against the
digital catalog instead of `ALL_TOPOLOGY_VALUES`, allows `kind: "iface"` in
`add_block`, and the consultant prompt says it is discussing the digital
microarchitecture (and receives the AFE architecture as read-only context so
it keeps the two consistent). Everything else (patch ops, transcript,
apply/undo) is unchanged.

---

## (c) AFE<->digital interface workspace (core feature)

The third workspace: a structured signal-table editor defining the contract
between the AFE diagram and the digital diagram. One interface definition
per PHY type (persisted like architectures; see storage below).

### Data model (the single source of truth)

```json
{
  "name": "serdes-rx-if",
  "phy_type": "ser-des",
  "clock_domains": [
    {"name": "clk_ref", "freq_mhz": 100, "owner": "external",
     "source_block": null, "description": "Board reference; PLL input and CSR/host domain."},
    {"name": "clk_rx_word", "freq_mhz": 100, "owner": "afe",
     "source_block": "deserializer", "description": "CDR-recovered word clock (line/16). Plesiochronous to clk_ref (+/-300 ppm)."}
  ],
  "signals": [
    {"name": "rx_pdata", "direction": "a2d", "width": 16,
     "clock_domain": "clk_rx_word", "rate": "word", "rate_mhz": 100,
     "group": "data", "handshake": "none",
     "afe_block": "deserializer", "digital_block": "rx-align",
     "meaning": "Deserialized parallel data, bit 0 received first."}
  ],
  "cdc_points": [
    {"signal": "cdr_locked", "from_domain": "async", "to_domain": "clk_ref",
     "strategy": "2ff", "notes": "Level signal; 2-flop synchronizer."}
  ],
  "reset_sequence": [ {"step": 1, "action": "...", "signal": "...", "wait_for": "..."} ],
  "power_sequence": [ {"step": 1, "action": "...", "signal": "...", "wait_for": "..."} ]
}
```

Field enums (validated server-side, rendered as dropdowns in the table
editor):

- `direction`: `"a2d"` (AFE -> digital) | `"d2a"` (digital -> AFE).
- `rate` (toggle class): `"line"` (per-UI - should not normally cross the
  boundary; the tool WARNS if a `line`-rate signal appears, since the whole
  point of the deserializer is that only word-rate signals cross),
  `"word"` (word/parallel clock), `"quasi_static"` (trim/config - changes
  only under a documented hold condition), `"async_event"` (level/pulse
  status, no clock). `rate_mhz` optional number, required when rate is
  `"word"`.
- `group`: `"data" | "clock" | "control" | "status" | "reset" | "power"` -
  the table renders one section per group, in that order.
- `handshake`: `"none"` (source-synchronous, always valid),
  `"valid"` (qualified by a valid strobe), `"valid_ready"`,
  `"req_ack"` (4-phase), `"level"` (static level, e.g. enables),
  `"pulse"` (single-cycle event - must be stretched/synced if it crosses
  domains), `"2ff_sync"` (async level, consumer synchronizes).
- `clock_domain`: must name an entry in `clock_domains`, or `"async"`.
- `afe_block` / `digital_block`: optional block ids from the two diagrams -
  the cross-link that lets the UI highlight both endpoints and lets the
  consultant sanity-check the tables against the architectures. Dangling ids
  are a soft warning (blocks get renamed/removed), never a hard error.
- `name`: `[a-z][a-z0-9_]*`, unique; `width`: int >= 1 (rendered as
  `name[width-1:0]` when width > 1).

Validation (server + mirrored lightweight in the editor):
hard errors - duplicate/invalid names, unknown enum values, unknown
clock_domain, cdc_point referencing a nonexistent signal/domain, width < 1.
Soft warnings (shown inline, never blocking) - `line`-rate signal crossing
the boundary; an `async`/`async_event` signal with no cdc_point and
handshake not `2ff_sync`; a `pulse` crossing domains without a cdc_point;
a `d2a` `quasi_static` bus not mentioned in any FSM/cal block's
`adapt_targets`-style field; word-rate `rate_mhz` above 200 (sky130
synthesized-logic warning, same text as the catalog fields).

### Worked example: SerDes RX interface (ships as the built-in default for `ser-des`)

Rates chosen for sky130: 1.6 Gb/s NRZ line rate, half-rate CDR (800 MHz
internal to the AFE - never crosses), 1:16 deserializer -> 100 MHz word
clock, 100 MHz reference. Digital core at 100 MHz.

Clock domains:

| name | freq_mhz | owner | source_block | description |
|---|---|---|---|---|
| clk_ref | 100 | external | - | Board reference. PLL input; CSR/host + sequencer domain. |
| clk_rx_word | 100 | afe | deserializer | CDR-recovered word clock (1.6 GHz line / 16). Plesiochronous to clk_ref, +/-300 ppm -> elastic FIFO in the digital datapath. |

Signal table (`serdes-rx-if`):

| name | dir | width | clock_domain | rate | meaning | handshake |
|---|---|---|---|---|---|---|
| **data** |
| rx_pdata | a2d | 16 | clk_rx_word | word (100 MHz) | Deserialized parallel data, LSB = first bit on the wire. Garbage until cdr_locked. | none (source-sync) |
| rx_err_sample | a2d | 1 | clk_rx_word | word (100 MHz) | Error-slicer decision for eye monitor / sign-sign adaptation (optional). | valid (rx_err_valid) |
| rx_err_valid | a2d | 1 | clk_rx_word | word (100 MHz) | Qualifies rx_err_sample (error slicer strobes only when enabled). | none |
| **clock** |
| clk_rx_word | a2d | 1 | - (is a clock) | word (100 MHz) | Recovered word clock; times every a2d data signal above. Digital treats it as a clock input, never gates it. | none |
| clk_ref | ext | 1 | - (is a clock) | 100 MHz | Reference to AFE PLL and digital host domain. | none |
| **control** |
| ctle_peak | d2a | 4 | clk_ref | quasi_static | CTLE peaking code (0-15). Hold rule: change only while adapt_hold=1 or en_rx_dig=0. | level |
| vga_gain | d2a | 5 | clk_ref | quasi_static | VGA gain code (0-31). Same hold rule. | level |
| dfe_tap1 | d2a | 6 | clk_ref | quasi_static | DFE tap-1 weight, sign-magnitude (bit5 = sign). | level |
| slicer_ofst | d2a | 6 | clk_ref | quasi_static | Data-slicer offset trim, sign-magnitude, ~1 mV/LSB. | level |
| eye_vref_code | d2a | 6 | clk_ref | quasi_static | Error-slicer threshold DAC code (eye monitor vertical axis). | level |
| eye_phase_code | d2a | 6 | clk_ref | quasi_static | PI code for error-slicer sampling phase (eye monitor horizontal axis, 64 steps/UI). | level |
| adapt_hold | d2a | 1 | clk_ref | quasi_static | Freeze AFE-internal adaptation while digital rewrites trim codes. | level |
| **status** |
| pll_locked | a2d | 1 | async | async_event | PLL lock detector. Gates power-up sequencing step 3. | 2ff_sync (-> clk_ref) |
| cdr_locked | a2d | 1 | async | async_event | CDR lock/valid-data indication. Gates deser reset release and word alignment start. | 2ff_sync (-> clk_ref) |
| sig_detect | a2d | 1 | async | async_event | Input amplitude above threshold (loss-of-signal inverse). | 2ff_sync (-> clk_ref) |
| **reset** |
| rstn_por | ext | 1 | async | async_event | Chip POR, async assert. Root of both sequences below. | 2ff_sync per domain |
| deser_rstn | d2a | 1 | clk_rx_word | async_event | Deserializer/word-counter reset: async assert, release synchronized into clk_rx_word by the AFE. | level |
| **power** |
| en_afe_bias | d2a | 1 | clk_ref | quasi_static | Master bias enable (bandgap, bias mirrors). First on, last off. | level |
| en_pll | d2a | 1 | clk_ref | quasi_static | PLL enable. | level |
| en_rx_fe | d2a | 1 | clk_ref | quasi_static | RX frontend enable (term/VGA/CTLE/DFE/slicer). | level |
| en_cdr | d2a | 1 | clk_ref | quasi_static | CDR enable. | level |

CDC points:

| signal | from_domain | to_domain | strategy | notes |
|---|---|---|---|---|
| pll_locked | async | clk_ref | 2ff | level |
| cdr_locked | async | clk_ref | 2ff | level |
| sig_detect | async | clk_ref | 2ff | level |
| rx_pdata (+err) | clk_rx_word | core clock | async_fifo | This IS the elastic FIFO block in the digital diagram - +/-300 ppm absorbed by SKP insert/delete. |
| trim buses | clk_ref | (AFE, unclocked) | quasi_static | Safe only under the adapt_hold rule above. |

Reset/power sequence (one ordered list drives the `power_state_fsm` spec):

| step | action | signal | wait_for |
|---|---|---|---|
| 1 | POR released, all enables 0, deser_rstn=0 | rstn_por | - |
| 2 | Enable bias | en_afe_bias=1 | 20 us settle |
| 3 | Enable PLL | en_pll=1 | pll_locked (timeout 500 us -> ERROR) |
| 4 | Enable RX frontend | en_rx_fe=1 | 5 us settle |
| 5 | Enable CDR | en_cdr=1 | cdr_locked (timeout 1 ms -> retry from 4, 3x then ERROR) |
| 6 | Release deserializer | deser_rstn=1 | 16 word clocks |
| 7 | Start word alignment / training | (digital-internal) | aligner lock |

Power-down is the reverse order; sleep = steps 5-7 undone only.

### UI: interface table editor

Middle workspace panel. Renders the definition as grouped tables (one per
`group`), each row editable in place (text inputs + the enum dropdowns
above), add-row per group, delete per row. Below the signal table: the
clock-domains table, CDC table, and the two sequence tables (reorderable
rows). Inline soft warnings per row (same visual language as the spec form's
sky130 warnings). Buttons: Save / Load (see storage), Undo last patch,
Export JSON (download of the definition verbatim). Clicking a row with
`afe_block`/`digital_block` set highlights those blocks in the flanking
diagrams (v1: only if cheap; otherwise defer - the ids alone already carry
the value).

### Storage & API (mirrors architectures exactly)

- `backend/interfaces.py` + settings root `interfaces/`:
  `<working_dir>/interfaces/<name>.json`.
- `GET /api/interfaces` -> `{interfaces: [{name, label, phy_type, created_at,
  updated_at, interface: {...}}]}` newest first across working dirs.
- `POST /api/interfaces` -> same name rules as architectures
  (1-64 `[A-Za-z0-9_-]`, existing name = overwrite), 422 on validation
  errors (hard errors only; warnings are returned alongside as
  `warnings: [...]` and stored with the entry, like `compute_soft_warnings`).
- Working copy in localStorage per phy: `if-def-<phyType>`; built-in default
  per phy ships in `frontend/src/defaultInterfaces.js` (the worked example
  above is the `ser-des` default; the other PHYs get analogous defaults -
  LPDDR's uses DFI-style names: `dfi_address`, `dfi_wrdata`, `dfi_rddata`,
  `dfi_rddata_valid`, `dfi_init_complete`, DLL/PI code buses, `zq_code`).

### Chat integration: the interface consultant

`POST /api/if_chat` - same module pattern, request/response envelope,
session rules, transcript persistence (`if_chats/` root), timeout, and
error semantics as `/api/arch_chat` (2026-08-21 contract). Differences only:

Request body:

```json
{
  "session_id": "ifchat-abc123",
  "message": "The trim buses have no hold rule - is that safe?",
  "interface": { ...the full definition exactly as the editor holds it... },
  "afe_architecture": {"blocks": [...], "edges": [...]},
  "digital_architecture": {"blocks": [...], "edges": [...]},
  "phy_type": "ser-des"
}
```

The consultant prompt must state: both architectures are READ-ONLY context
(it may recommend arch changes in prose but its patch can only touch the
interface); sky130 rate constraints (warn beyond 1-2 Gb/s line,
~200 MHz synthesized logic); the enum vocabularies above; and the review
duties - every a2d/d2a signal accounted for by some block pair, every async
crossing has a CDC entry, sequences gate on status signals not bare waits
where a status signal exists, no line-rate signal crosses the boundary.

Patch ops (ordered, applied in order; same fenced-JSON extraction, same
`patch_valid`/`patch_errors`/never-fail-the-chat rules):

```json
{"op": "add_signal", "signal": { ...complete signal record... }}
{"op": "remove_signal", "name": "<existing signal name>"}
{"op": "update_signal", "name": "<existing>", "fields": { ...partial record, name change via "name" in fields... }}
{"op": "add_clock_domain", "domain": { ...complete domain record... }}
{"op": "remove_clock_domain", "name": "<existing>"}
{"op": "update_clock_domain", "name": "<existing>", "fields": { ... }}
{"op": "add_cdc", "cdc": { ...complete cdc record... }}
{"op": "remove_cdc", "signal": "<signal name of an existing cdc entry>"}
{"op": "set_reset_sequence", "steps": [ ...complete replacement list... ]}
{"op": "set_power_sequence", "steps": [ ...complete replacement list... ]}
```

Sequences are whole-list replacement by design (ordered lists diff badly;
the consultant sees the current list and returns the new one). Server-side
validation = the hard-error rules above evaluated against the interface
state as the op sequence progresses (an `add_signal` name may be referenced
by a later `add_cdc` in the same patch); `remove_signal` implicitly removes
its cdc entries (the client applier must do the same);
`remove_clock_domain` is rejected while any signal still references the
domain. Frontend applier + snapshot/undo mirror
`applyArchOps`/`archSnapshot` against the `if-def-<phy>` record.

---

## (d) Start screen

Extend the existing empty start screen (2026-08-20 cycle-2 screenshot) -
after the PHY type is chosen, the workbench shows THREE side-by-side
workspace panels in one row:

```
[ PHY / AFE architecture ]  [ AFE <-> Digital interface ]  [ Digital architecture ]
```

- Left and right are the two diagram views (existing `ArchitectureDiagram`
  with the two data sources/namespaces). Middle is the interface table
  editor.
- On load, all three render their defaults for the selected PHY. One panel
  is "focused" (wider, e.g. 60%) at a time; clicking a collapsed panel's
  header focuses it. Below ~1100 px they stack vertically (same breakpoint
  the chat sidebar already uses).
- ONE chat sidebar, context-switching with the focused panel: AFE focus ->
  `/api/arch_chat` (view "afe"), digital focus -> `/api/arch_chat`
  (view "digital"), interface focus -> `/api/if_chat`. Three separate
  session ids (`arch-chat-session-<phy>`, `dig-chat-session-<phy>`,
  `if-chat-session-<phy>`) so transcripts stay coherent per workspace.
- No new routing, no wizard, no dashboard. That is the whole start screen.

---

## (e) v1 scope recommendation

IN (this cycle):

1. `backend/digital_blocks.py` + `GET /api/digital_blocks` (catalog (a),
   fields, per-PHY map, model-applicability entries with reasons).
2. `frontend/src/digitalArchitectures.js` (diagrams (b)) + namespace-
   parameterized reuse of the phyArchitectures helper layer +
   `ArchitectureDiagram` rendering of the digital view; `ddr`/`hbm` alias
   `lpddr`.
3. `arch_chat` `view` parameter (digital-catalog validation + digital
   prompt framing).
4. Interface data model + validation + `backend/interfaces.py` +
   `GET/POST /api/interfaces`; `defaultInterfaces.js` with the SerDes RX
   worked example as the shipped `ser-des` default (LPDDR/D2D/optical
   defaults can start smaller - clocks, resets, and 6-10 headline signals).
5. Interface table editor panel + three-panel start screen + context-
   switched chat with `POST /api/if_chat` (patch ops, apply/undo).

OUT (explicitly deferred - do not build speculatively):

- RTL generation of any digital block (catalog + fields only; the
  `verilog`/`rnm` applicability entries are forward hooks, not a promise -
  wiring digital blocks into the Circuit_Builder run flow is its own
  future spec).
- Register-map generation (SystemRDL/CSV export) from the CSR block.
- SDC/timing constraints, synthesis, or any OpenLane integration.
- Diagram-row <-> table-row live highlighting if it costs more than a day.
- Dedicated `ddr`/`hbm` digital diagrams and interface defaults.
- Protocol-engine behavioral content (the `standard` field is
  documentation).

Sequencing note for Analog_Tool_Dev/Graphics_Dev: items 1+4 (backend) are
independent of 2+5 (frontend) and can land first with tests mirroring
`test_arch_chat.py` (patch validation table-driven; stubbed consultant;
save/list/overwrite round trip; the worked-example default must round-trip
through POST /api/interfaces with zero hard errors and exactly the soft
warnings documented above - i.e. none).

---

## Implementation status - v1 backend + workspace + layout (Analog_Tool_Dev, 2026-08-23)

Landed (spec items 1, 3, 4, 5 in full; item 2's data + store layer, rendering
handed to Graphics_Dev - contract below):

**Backend**
- `backend/digital_blocks.py` + `GET /api/digital_blocks` -> `{groups, by_phy}`:
  full catalog (a) with fields exactly as specified, model-applicability
  entries (veriloga disabled with the "no compact-model behavior" reason for
  every block; verilog True for all; rnm True for exactly link_training_fsm /
  power_state_fsm / cal_engine / eye_monitor_ctrl / ddr_training_engine /
  zq_cal_fsm / d2d_link_state_fsm), and `DIGITAL_BLOCKS_BY_PHY`. Separate
  namespace - nothing added to `TOPOLOGY_GROUPS` (asserted by test).
- `arch_chat` `view: "afe"|"digital"` (default "afe" - pre-existing behavior
  bit-identical): digital view validates patch topologies against the digital
  catalog, allows `kind:"iface"` (and now rejects `kind` values foreign to a
  view: "pad" afe-only, "iface" digital-only), takes `afe_architecture` as
  READ-ONLY prompt context, uses a digital-microarchitecture prompt framing
  with the sky130 50-200 MHz guidance. Off-per-PHY `add_block` topologies get
  a per-op `"warning"` (soft, response `patch_warnings`), never a rejection.
- `backend/interfaces.py`: data model normalization; validation split into
  hard errors (duplicate/invalid names, unknown enums, unknown clock_domain,
  dangling cdc refs, width < 1, missing rate_mhz on word-rate) vs soft
  warnings (line-rate crossing, async signal w/o cdc entry unless 2ff_sync,
  pulse w/o cdc entry, word clock > 200 MHz, dangling afe_block/digital_block
  when the diagrams are supplied); the 10 chat patch ops applied in order
  against a working copy with the cascade semantics (remove_signal drops its
  cdc entries; update_signal/update_clock_domain renames cascade into
  references; remove_clock_domain rejected while referenced by a signal OR a
  cdc entry). Storage `<working_dir>/interfaces/<name>.json` mirroring
  architectures.py (roots search, overwrite preserves created_at, warnings
  stored with the entry). `backend/settings.py` grew `interfaces` + `if_chats`
  roots.
- `backend/if_chat.py` + `POST /api/if_chat` / `GET /api/if_chat/{session}`:
  same envelope/session/timeout/502 semantics as arch_chat (delegates to the
  same consultant invocation via a separately monkeypatchable
  `run_if_consultant`); transcripts under `if_chats/`; response additionally
  carries `interface_warnings` computed on the post-patch state.
- `GET/POST /api/interfaces` per spec (422 = hard errors only, formatted list).

**Frontend**
- `phyArchitectures.js` factored into `makeArchStore(prefix, archMap)`;
  the default `afeArchStore` keeps the exact pre-existing "arch-" keys (user
  edits survive); all old named exports preserved.
- `frontend/src/digitalArchitectures.js`: `DIGITAL_ARCHITECTURES` verbatim
  from section (b), `ddr`/`hbm` aliasing `lpddr`, `digitalArchStore` on the
  "dig-arch-" namespace (chat patches already apply/undo through it).
- `frontend/src/defaultInterfaces/*.json` + `defaultInterfaces.js`: the
  worked SerDes RX example ships as the `ser-des` default; lpddr (DFI names:
  dfi_address/dfi_wrdata/dfi_rddata/dfi_rddata_valid/dfi_init_complete,
  dll_pi_code, vref_dq_code, zq_code), die-to-die and optical starters;
  ddr/hbm alias lpddr. The JSON files are the single source of truth, shared
  with the backend round-trip tests.
- `frontend/src/interfaceStore.js`: `if-def-<phy>` working copy, client
  patch applier mirroring the server cascades, snapshot/undo, lightweight
  validation mirror for the inline row annotations.
- `frontend/src/InterfaceEditor.jsx`: grouped signal tables
  (data/clock/control/status/reset/power, add-row per group, delete per row,
  enum dropdowns, `name[w-1:0]` rendering), clock-domain / CDC / two
  reorderable sequence tables, inline hard errors (red) + soft warnings
  (amber, spec-form visual language), Save (POST /api/interfaces, warnings
  reported), Load (saved list), Reset to default, Export JSON, View JSON.
- Workbench in `App.jsx`. LAYOUT + NAMING REVISED per user requests
  (2026-08-23, mid-implementation, two rounds): instead of side-by-side
  columns, the panels are STACKED one below the other at full width, in the
  signal-path order

      PHY architecture (top-level overview)
      -> PHY / AFE architecture
      -> AFE <-> Controller interface
      -> PHY / Controller architecture

  each individually minimizable to its header bar (Minimize/Expand button,
  which does not steal focus). Clicking a panel header focuses it (accent
  border + "chat follows this panel") and the chat sidebar context-switches
  with the focused panel, as specified. "Controller" is the DISPLAY name for
  the digital side everywhere in the UI (panel titles, chat titles/hints);
  internal ids, per-workspace session ids, localStorage namespaces and the
  API `view="digital"` are UNCHANGED, so the Graphics_Dev contract and all
  stored data stay valid.
- `frontend/src/PhyOverviewPanel.jsx` (new, user request): the top "PHY
  architecture" panel depicting the whole PHY as its two halves -
  [Line/channel] <-> **AFE** <-> IF <-> **Controller** <-> [SoC core/host].
  Static + navigational only (no state/chat of its own): clicking the AFE,
  IF or Controller block focuses the corresponding workspace (the chat
  follows); the focused block carries the same accent as the focused panel.
- `DigitalArchPanel.jsx` started as a live-data placeholder over
  `digitalArchStore`; Graphics_Dev's diagram rendering (ArchitectureDiagram
  variant="digital") landed mid-cycle and replaced it - the browser checks
  below now assert against the rendered SVG diagram (teal digital blocks,
  indigo "boundary IF" anchors, chat patches appearing as diagram nodes).
- One context-switching chat sidebar (`ArchChatSidebar.jsx`): AFE focus ->
  arch_chat view "afe", digital focus -> arch_chat view "digital" (+ AFE
  read-only context), interface focus -> if_chat (interface + both
  architectures). Three separate persisted session ids per phy:
  `arch-chat-session-<phy>` / `dig-chat-session-<phy>` / `if-chat-session-<phy>`.
  Apply/undo per workspace (diagram stores or the if-def working copy).

**Verification evidence**
- `backend/test_digital_if.py`: 52 stubbed tests in the test_arch_chat.py
  style - catalog shape/models/namespace/per-PHY map, view-aware patch
  validation (incl. kind rules + soft per-PHY warning), hard-vs-soft
  validation table-driven, all 10 patch ops (ordered refs, cascades,
  rejections), all four shipped defaults validate with zero hard errors and
  zero warnings, /api/interfaces round-trip + overwrite + 422 + stored
  warnings, /api/if_chat transcript/502/invalid-patch/session-id tests.
  All suites green: 76 pytest (24 pre-existing arch_chat + 52 new) + the five
  script-style suites under backend/tests/.
- Browser pass (playwright, chat network intercepted - $0): 48 checks all
  passing - overview + three stacked full-width panels in
  overview/AFE/interface/controller order with the renamed titles, overview
  block clicks navigate focus (and the chat follows), individual
  Minimize/Expand on all four panels (without stealing focus), header-click
  focus switching, seeded table, inline soft warning (250 MHz) + hard error
  (duplicate name) + revert, if_chat request payload (session id namespace,
  full definition, both architectures, phy_type), patch card -> Apply (table
  updates) -> Undo (reverts), controller chat sends view="digital"+AFE
  context and an applied add_block renders as a node in Graphics_Dev's
  digital diagram with its catalog topology, per-workspace session ids
  distinct, real (free) Save round-trip, narrow-viewport reflow.
  Screenshots: `2026-08-23-workbench-threepanel.png`,
  `2026-08-23-workbench-minimized.png`, `2026-08-23-if-editor-focused.png`,
  `2026-08-23-if-editor-validation.png`, `2026-08-23-ifchat-patch-card.png`,
  `2026-08-23-ifchat-patch-applied.png`, `2026-08-23-digital-panel-chat.png`,
  `2026-08-23-workbench-stacked.png`.
- ONE real if_chat smoke (authorized): seeded SerDes example, "is 2ff right
  for cdr_locked?" -> HTTP 200, $0.29, 11.7 s, technically sound reply,
  correctly no patch, `interface_warnings: []`, transcript persisted under
  `if_chats/`.

**Decisions / deviations (documented in code too)**
- `direction` enum includes `"ext"`: the spec prose says a2d|d2a but its own
  worked example (which must round-trip warning-free) marks clk_ref/rstn_por
  as ext.
- `rate_mhz` missing on a word-rate signal is a HARD error (spec: "required
  when rate is word").
- The worked example's single 7-step bring-up table maps to the data model's
  two lists: full bring-up in `power_sequence`, POR/deser-reset steps in
  `reset_sequence`.
- The "d2a quasi_static bus not in any adapt_targets" warning is a dormant
  hook (`validate_interface(adapt_targets_text=...)`): diagram blocks don't
  carry field VALUES today, so there is nothing to check against - wire it
  when block field values become part of a stored artifact.
- "Undo last patch" lives in the chat sidebar (where Apply is), one undo
  slot per workspace - not duplicated inside the editor toolbar.
- Diagram-row <-> table-row live highlighting deferred per the spec's own
  cost rule (the afe_block/digital_block ids are in the table).
- No "save digital architecture" button yet (saved-architecture storage/
  selector is AFE-shaped; extend when the digital diagram lands).

**Deferred (unchanged from section e)**: RTL generation, register-map
export, SDC/synthesis, dedicated ddr/hbm digital diagrams + defaults,
protocol-engine behavior.

### Graphics_Dev handoff - digital diagram rendering contract

Replace the placeholder body of `frontend/src/DigitalArchPanel.jsx` (mounted
by App.jsx in the `data-workspace="digital"` panel) with the real diagram.
Everything below already exists and is tested - do not invent parallel state:

- Props the panel receives from App.jsx:
  - `phyType: string` - current PHY (`archView.phy_type`).
  - `version: number` - bumps whenever the digital architecture was edited
    OUTSIDE the panel (chat patch Apply/Undo in the sidebar): re-read the
    store on change, exactly like ArchitectureDiagram's `version` prop.
- Data/store (frontend/src/digitalArchitectures.js):
  - `DIGITAL_ARCHITECTURES` - built-ins, PHY_ARCHITECTURES shape;
    `ddr`/`hbm` alias `lpddr` (edits are stored per phyType key, so safe).
  - `digitalArchStore` = `makeArchStore("dig-arch-", DIGITAL_ARCHITECTURES)`
    - the SAME API surface ArchitectureDiagram uses today from
    phyArchitectures.js (`blocksForPhy`, `effectiveEdgesForPhy`,
    `userBlocksKey`/`hiddenBlocksKey`/`blockOverridesKey`/`wireEditsKey`/
    `layoutKeyForPhy`, `archLevel`, `currentArchitecture`, `applyArchOps`,
    `archSnapshot`/`restoreArchSnapshot`, `loadArchitectureIntoPhy`), just
    namespaced "dig-arch-". Recommended approach per the spec: give
    ArchitectureDiagram a `store` prop defaulting to `afeArchStore` and pass
    `digitalArchStore` here - do NOT fork the component.
- Topology picker: fetch `getDigitalBlocks()` (frontend/src/api.js) ->
  `{groups: [{group, options: [{value,label,fields,models}]}], by_phy}`.
  Use `groups` for the picker optgroups (same shape as /api/topologies) and
  `by_phy[phyType]` to order/prefer the PHY's usual set (offering the rest
  is fine - the server only soft-warns).
- `kind: "iface"` nodes (`topology: null`): render/behave EXACTLY like the
  AFE view's `kind: "pad"` - drawn, draggable, wireable, NOT selectable as a
  design target.
- Block clicks must NOT feed the spec form / Circuit_Builder flow (digital
  blocks are never built in v1) - selection is visual only.
- The chat sidebar already applies digital patches through
  `digitalArchStore` and App already bumps `version` - if the diagram reads
  through the store on `version` change, chat edits render with zero extra
  wiring. Keep the placeholder's `DIGITAL_DESCRIPTIONS[phyType]` hint line.
- CSS hooks available: `.digital-arch-panel`; the stacked workbench gives
  the panel the full row width at all times, and the user can minimize it to
  its header (`.workspace-panel.minimized` - body unmounted, so mount-time
  work should be cheap).

### Graphics_Dev status - digital diagram rendering LANDED (2026-08-23)

Done per the contract above, one shared renderer, no component fork:

- `ArchitectureDiagram.jsx` gained `store` (default `afeArchStore` - AFE call
  sites and storage keys bit-identical), `variant` ("afe" default | "digital")
  and `topologyGroups` (optional optgroup-shaped picker data; flat
  `topologyOptions` unchanged). All storage access now goes through the store
  (`layoutKeyForPhy`/`wireEditsKey` included). `kind:"iface"` renders like
  `kind:"pad"` (rounded, wireable/draggable, never a design target) with its
  own indigo style + "boundary IF" subtitle. Digital variant accent:
  designable blocks teal (`#f0fdfa`/`#0d9488`) instead of the status palette,
  no build-status captions/legend (no digital build flow), select-only hint,
  PNG export prefixed `digital-`.
- `DigitalArchPanel.jsx`: placeholder replaced - mounts ArchitectureDiagram
  with `digitalArchStore` + `variant="digital"`, keeps the
  `DIGITAL_DESCRIPTIONS[phyType]` hint, local visual-only `selectedBlock`
  (click toggles; nothing feeds the spec form), fetches `getDigitalBlocks()`
  once and builds picker optgroups with the `by_phy[phyType]` typical set
  sorted first inside each group (rest still offered).
- `App.css`: dead placeholder rules removed; `.digital-arch-panel
  .arch-diagram-scroll` gets a teal border/canvas tint.
- Verified headless (playwright/chromium, $0, no chat calls): 33/33 checks -
  all four PHY diagrams render with exact block/iface/wire counts, iface
  styling + subtitles, teal accent, selection highlight + deselect + spec-form
  untouched, iface not selectable, catalog optgroups (off-per-PHY groups still
  offered), simulated chat patch (dig-arch- localStorage records + forced
  store re-read, same code path as the version bump) renders block + wire,
  AFE diagram unaffected. Screenshots:
  `2026-08-23-digital-diagram-{ser-des,lpddr,die-to-die,optical,selected,patched,workbench}.png`.
- Not done (unchanged): saved-architecture Save/Load UI for the digital view;
  dedicated ddr/hbm diagrams.
- 2026-08-23 follow-up: overview blocks now un-minimize + smooth-scroll their target panel into view (App.jsx handleOverviewFocus + refs), and the PHY Type/Architecture pull-down moved from SpecForm into the overview panel header (PhyTypeSelect.jsx, reachable while minimized) - checks in `backend/tests/browser_check_overview.py` (27/27).
