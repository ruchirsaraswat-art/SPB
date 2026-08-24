// Digital-architecture diagrams (2026-08-23 digital-arch spec, section b):
// the built-in digital microarchitecture per PHY type, in EXACTLY the
// PHY_ARCHITECTURES data shape ({blocks: [{id,label,short,topology,col,row,
// kind?}], edges: [{from,to}]}). `topology` values reference the DIGITAL
// block catalog (GET /api/digital_blocks), not the analog one; `topology:
// null` + `kind: "iface"` marks boundary anchor nodes (AFE side, core side,
// host bus) - drawn, wireable, not selectable, exactly like kind "pad" in
// the AFE view.
//
// The localStorage edit-record layer is the SAME hierarchy-aware store as
// the AFE view, namespaced "dig-arch-" (see makeArchStore in
// phyArchitectures.js) - chat patches (arch_chat view="digital") apply
// through digitalArchStore.applyArchOps and the renderer reads
// digitalArchStore.blocksForPhy / effectiveEdgesForPhy.
//
// Rendering: ArchitectureDiagram reuse (storage namespace + digital catalog
// for the topology picker) is Graphics_Dev's slice - see the handoff
// contract appended to audits/2026-08-23-digital-arch-spec.md.

import { makeArchStore } from "./phyArchitectures";

export const DIGITAL_DESCRIPTIONS = {
  "ser-des": "Word-clock datapath (align, decode, rate-match, frame) + training/CSR control plane",
  "ddr": "DFI-shaped command/write/read channels toward the memory controller (reuses the LPDDR digital architecture in v1)",
  "lpddr": "DFI-shaped command/write/read channels toward the memory controller",
  "hbm": "DFI-shaped channels toward the memory controller (reuses the LPDDR digital architecture in v1; add lane repair when needed)",
  "optical": "SerDes-like word-clock datapath + TIA/LA calibration engine and LOS handling",
  "die-to-die": "UCIe-flavored mainband datapath with deskew + repair, always-on sideband, LTSM",
};

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

// "ddr" and "hbm": v1 reuses the "lpddr" digital architecture (DDR:
// identical structure; HBM: add lane_repair_mux between rd/wr paths and the
// AFE ifaces when someone asks). Shallow reuse is fine - user edits are
// stored per phyType key ("dig-arch-...-ddr:..." etc.), not on the shared
// object.
DIGITAL_ARCHITECTURES.ddr = DIGITAL_ARCHITECTURES.lpddr;
DIGITAL_ARCHITECTURES.hbm = DIGITAL_ARCHITECTURES.lpddr;

// The digital view's edit-record store: same helper layer as the AFE view,
// separate "dig-arch-" localStorage namespace. This is what the digital chat
// applies patches through and what the (Graphics_Dev) renderer reads.
export const digitalArchStore = makeArchStore("dig-arch-", DIGITAL_ARCHITECTURES);
