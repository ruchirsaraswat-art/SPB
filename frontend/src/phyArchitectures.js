// Single source of truth for each PHY type's block-level architecture:
// the blocks it's made of, each block's default grid position (col/row),
// which backend topology category (backend/topologies.py) each maps to, and
// the directed signal-flow edges between them. Used by
// PhyArchitectureSelector/ArchitectureDiagram (the wired diagram + block
// selector) and SpecForm (block -> topology mapping).
//
// Architectures researched/validated by Circuit_Researcher (2026-08-19) -
// see ~/.claude/agent-knowledge/circuit-topologies/phy-block-architectures.md
// for sources and rationale. Key structural facts encoded here:
//   - every PHY drives/receives through explicit PAD nodes;
//   - half-duplex buses (DDR/LPDDR/HBM DQ) hang the TX driver and RX path
//     off the SAME bidirectional pad, never in series;
//   - source-synchronous interfaces (DDR/LPDDR/HBM, die-to-die) are clocked
//     by a forwarded strobe/clock, not a CDR; die-to-die (UCIe-style)
//     explicitly has NO CDR;
//   - serdes/optical CDR taps the equalized signal and clocks the sampler.
//
// `topology: null` marks a node that can't be designed in this analog flow:
// PAD nodes (kind: "pad"), photodiodes, digital calibration logic. They're
// drawn for architectural completeness and can be wired to, but not selected.

export const PHY_DESCRIPTIONS = {
  "ser-des": "High-speed serializer/deserializer for chip-to-chip communication",
  "ddr": "Double-data-rate DRAM interface with source-sync clocking",
  "lpddr": "Low-power double-data-rate mobile memory interface",
  "hbm": "High-bandwidth memory interface (3D stacked)",
  "optical": "Optical I/O with transimpedance and limiting amplifiers",
  "die-to-die": "Chiplet-to-chiplet interconnect interface",
};

export const PHY_ARCHITECTURES = {
  "ser-des": {
    // Full duplex: one RX lane + one TX lane with separate pads. CDR taps
    // the CTLE output and clocks the sampler; PLL clocks serializer + CDR.
    blocks: [
      { id: "rx-pad", label: "RX PAD", short: "RX PAD", topology: null, kind: "pad", col: 0, row: 0 },
      {
        id: "rx-frontend",
        label: "RX Termination & Input Buffer",
        short: "RX Term/Buf",
        topology: "input_termination_buffer",
        col: 1,
        row: 0,
        // Hierarchical: drill in to design the frontend's internals.
        children: {
          blocks: [
            { id: "term", label: "Input Termination", short: "Termination", topology: "input_termination_buffer", col: 0, row: 0 },
            { id: "vga", label: "Variable Gain Amplifier", short: "VGA", topology: "vga", col: 1, row: 0 },
          ],
          edges: [{ from: "term", to: "vga" }],
        },
      },
      {
        id: "rx-eq",
        label: "RX Equalizer (CTLE + DFE)",
        short: "RX Equalizer",
        topology: "ctle",
        col: 2,
        row: 0,
        children: {
          blocks: [
            { id: "ctle-stage", label: "CTLE Stage", short: "CTLE", topology: "ctle", col: 0, row: 0 },
            { id: "dfe", label: "Decision Feedback Equalizer", short: "DFE", topology: "dfe", col: 1, row: 0 },
          ],
          edges: [{ from: "ctle-stage", to: "dfe" }],
        },
      },
      { id: "sampler", label: "Sampler / Slicer", short: "Sampler", topology: "sense_amp_slicer", col: 3, row: 0 },
      { id: "deserializer", label: "Deserializer", short: "Deserializer", topology: "deserializer", col: 4, row: 0 },
      { id: "pll", label: "PLL (Lane Clock Synthesis)", short: "PLL", topology: "pll", col: 2, row: 1 },
      { id: "cdr", label: "CDR (Clock & Data Recovery)", short: "CDR", topology: "cdr", col: 3, row: 1 },
      { id: "tx-pad", label: "TX PAD", short: "TX PAD", topology: null, kind: "pad", col: 0, row: 2 },
      { id: "tx-driver", label: "TX Output Driver", short: "TX Driver", topology: "output_driver", col: 1, row: 2 },
      { id: "tx-ffe", label: "TX FFE (Pre-emphasis)", short: "TX FFE", topology: "ffe", col: 2, row: 2 },
      { id: "serializer", label: "Serializer", short: "Serializer", topology: "serializer", col: 3, row: 2 },
    ],
    edges: [
      { from: "rx-pad", to: "rx-frontend" },
      { from: "rx-frontend", to: "rx-eq" },
      { from: "rx-eq", to: "sampler" },
      { from: "sampler", to: "deserializer" },
      { from: "rx-eq", to: "cdr" },
      { from: "cdr", to: "sampler" },
      { from: "pll", to: "cdr" },
      { from: "pll", to: "serializer" },
      { from: "serializer", to: "tx-ffe" },
      { from: "tx-ffe", to: "tx-driver" },
      { from: "tx-driver", to: "tx-pad" },
    ],
  },
  "ddr": {
    // Half-duplex: TX driver and ODT+receiver share one bidirectional DQ
    // pad. Source-synchronous: forwarded DQS strobe times the Vref-compared
    // receiver (DQS is bidirectional in reality, shown RX-only).
    blocks: [
      { id: "dq-pad", label: "DQ PAD (bidirectional)", short: "DQ PAD", topology: null, kind: "pad", col: 0, row: 0 },
      { id: "ddr-termination", label: "On-die Termination (ODT)", short: "ODT", topology: "input_termination_buffer", col: 1, row: 0 },
      { id: "ddr-receiver", label: "DQ Receiver (Vref Sense Amp)", short: "Receiver", topology: "sense_amp_slicer", col: 2, row: 0 },
      { id: "ddr-driver", label: "DQ Output Driver", short: "Driver", topology: "output_driver", col: 1, row: 1 },
      { id: "ddr-ref", label: "Vref Generator (VrefDQ)", short: "Vref Gen", topology: "bandgap_reference", col: 2, row: 1 },
      { id: "dqs-pad", label: "DQS Strobe PAD", short: "DQS PAD", topology: null, kind: "pad", col: 0, row: 2 },
      // DDR PHY strobe timing is conventionally DLL/phase-interpolator based
      // (90-degree DQS shift, per-lane deskew), not a plain buffer tree.
      { id: "ddr-dqs", label: "DQS Strobe RX & Deskew (DLL/PI)", short: "DQS DLL", topology: "dll_phase_interpolator", col: 1, row: 2 },
      { id: "ddr-pll", label: "PHY PLL (TX Clocking)", short: "PLL", topology: "pll", col: 2, row: 2 },
    ],
    edges: [
      { from: "ddr-driver", to: "dq-pad" },
      { from: "dq-pad", to: "ddr-termination" },
      { from: "ddr-termination", to: "ddr-receiver" },
      { from: "ddr-ref", to: "ddr-receiver" },
      { from: "dqs-pad", to: "ddr-dqs" },
      { from: "ddr-dqs", to: "ddr-receiver" },
      { from: "ddr-pll", to: "ddr-driver" },
    ],
  },
  "lpddr": {
    // Half-duplex LVSTL DQ: TX driver and Vref-based RX buffer share one DQ
    // pad; no standalone ODT block (LVSTL termination is the far-end driver
    // pull-down). SoC-side view: CA is unidirectional TX with its own pad.
    blocks: [
      { id: "dq-pad", label: "DQ PAD (bidirectional)", short: "DQ PAD", topology: null, kind: "pad", col: 0, row: 0 },
      { id: "lpddr-rx", label: "DQ RX Buffer (Vref-based)", short: "RX Buffer", topology: "input_termination_buffer", col: 1, row: 0 },
      { id: "lpddr-vref", label: "Vref Generator", short: "Vref Gen", topology: "bandgap_reference", col: 2, row: 0 },
      { id: "lpddr-tx", label: "DQ TX Driver (LVSTL)", short: "TX Driver", topology: "output_driver", col: 1, row: 1 },
      { id: "dqs-pad", label: "DQS/WCK Strobe PAD", short: "DQS PAD", topology: null, kind: "pad", col: 0, row: 2 },
      // Same as DDR: strobe timing (90-degree shift, per-lane deskew) is
      // DLL/phase-interpolator territory, not a plain buffer tree.
      { id: "lpddr-dqs", label: "Strobe RX & Deskew (DLL/PI)", short: "Strobe DLL", topology: "dll_phase_interpolator", col: 1, row: 2 },
      { id: "lpddr-pll", label: "PHY PLL", short: "PLL", topology: "pll", col: 2, row: 2 },
      { id: "ca-pad", label: "CA PAD (Command/Address)", short: "CA PAD", topology: null, kind: "pad", col: 0, row: 3 },
      { id: "lpddr-ca", label: "CA TX Driver", short: "CA Driver", topology: "output_driver", col: 1, row: 3 },
    ],
    edges: [
      { from: "lpddr-tx", to: "dq-pad" },
      { from: "dq-pad", to: "lpddr-rx" },
      { from: "lpddr-vref", to: "lpddr-rx" },
      { from: "dqs-pad", to: "lpddr-dqs" },
      { from: "lpddr-dqs", to: "lpddr-rx" },
      { from: "lpddr-pll", to: "lpddr-tx" },
      { from: "lpddr-pll", to: "lpddr-ca" },
      { from: "lpddr-ca", to: "ca-pad" },
    ],
  },
  "hbm": {
    // One lane of a very wide unterminated single-ended interposer bus:
    // bidirectional DQ microbump shared by a low-swing driver and a Vref
    // pseudo-differential sense-amp RX, no equalization. Forwarded
    // WDQS/RDQS strobe clocks RX/deserializer; calibration trims driver
    // impedance and per-lane deskew.
    blocks: [
      { id: "hbm-vref", label: "Vref Generator", short: "Vref Gen", topology: "bandgap_reference", col: 1, row: 0 },
      { id: "dq-pad", label: "DQ Microbump PAD (bidirectional)", short: "DQ PAD", topology: null, kind: "pad", col: 0, row: 1 },
      { id: "hbm-rx", label: "DQ RX (Vref Pseudo-diff Sense Amp)", short: "DQ RX", topology: "sense_amp_slicer", col: 1, row: 1 },
      { id: "hbm-deser", label: "Deserializer (Gearbox)", short: "Deserializer", topology: "deserializer", col: 2, row: 1 },
      { id: "hbm-tx", label: "DQ TX Driver (Low-swing, Unterminated)", short: "DQ TX", topology: "output_driver", col: 1, row: 2 },
      { id: "hbm-vserdes", label: "TX Serializer (Gearbox)", short: "Serializer", topology: "serializer", col: 2, row: 2 },
      { id: "hbm-pll", label: "PHY PLL", short: "PLL", topology: "pll", col: 3, row: 2 },
      { id: "ck-pad", label: "WDQS/RDQS Strobe Bump", short: "Strobe PAD", topology: null, kind: "pad", col: 0, row: 3 },
      { id: "hbm-clk", label: "Strobe Buffer / Distribution & Deskew", short: "Strobe Dist.", topology: "clock_buffer_distribution", col: 1, row: 3 },
      { id: "hbm-cal", label: "Calibration / Training", short: "Cal / Train", topology: null, col: 3, row: 3 },
    ],
    edges: [
      { from: "hbm-vserdes", to: "hbm-tx" },
      { from: "hbm-tx", to: "dq-pad" },
      { from: "dq-pad", to: "hbm-rx" },
      { from: "hbm-rx", to: "hbm-deser" },
      { from: "hbm-vref", to: "hbm-rx" },
      { from: "ck-pad", to: "hbm-clk" },
      { from: "hbm-clk", to: "hbm-rx" },
      { from: "hbm-clk", to: "hbm-deser" },
      { from: "hbm-pll", to: "hbm-vserdes" },
      { from: "hbm-cal", to: "hbm-tx" },
      { from: "hbm-cal", to: "hbm-rx" },
    ],
  },
  "optical": {
    // RX: photodiode current into TIA, CTLE compensating PD/TIA rolloff,
    // limiting amp, then decision slicer clocked by a CDR tapping the
    // limiting-amp output. TX: serializer -> laser/modulator driver -> pad.
    // Full duplex over separate fibers.
    blocks: [
      { id: "pd", label: "Photodiode", short: "Photodiode", topology: null, col: 0, row: 0 },
      { id: "tia", label: "Transimpedance Amplifier (TIA)", short: "TIA", topology: "tia", col: 1, row: 0 },
      { id: "optical-eq", label: "Equalizer (CTLE, PD/TIA Rolloff)", short: "Equalizer", topology: "ctle", col: 2, row: 0 },
      { id: "limiting-amp", label: "Limiting Amplifier / VGA", short: "Limiting Amp", topology: "vga", col: 3, row: 0 },
      { id: "optical-slicer", label: "Decision Circuit (Slicer)", short: "Slicer", topology: "sense_amp_slicer", col: 4, row: 0 },
      { id: "optical-pll", label: "PLL (Clock Synthesis)", short: "PLL", topology: "pll", col: 2, row: 1 },
      { id: "optical-cdr", label: "Optical CDR", short: "CDR", topology: "cdr", col: 3, row: 1 },
      { id: "tx-pad", label: "TX PAD (to Laser/Modulator)", short: "TX PAD", topology: null, kind: "pad", col: 0, row: 2 },
      { id: "mod-driver", label: "Laser / Modulator Driver", short: "Mod Driver", topology: "output_driver", col: 1, row: 2 },
      { id: "optical-ser", label: "Serializer", short: "Serializer", topology: "serializer", col: 2, row: 2 },
    ],
    edges: [
      { from: "pd", to: "tia" },
      { from: "tia", to: "optical-eq" },
      { from: "optical-eq", to: "limiting-amp" },
      { from: "limiting-amp", to: "optical-slicer" },
      { from: "limiting-amp", to: "optical-cdr" },
      { from: "optical-cdr", to: "optical-slicer" },
      { from: "optical-pll", to: "optical-cdr" },
      { from: "optical-pll", to: "optical-ser" },
      { from: "optical-ser", to: "mod-driver" },
      { from: "mod-driver", to: "tx-pad" },
    ],
  },
  "die-to-die": {
    // UCIe-style: unidirectional TX and RX lanes with a forwarded
    // differential clock in each direction, so there is NO CDR - the
    // received forwarded clock, delay-matched to the data path, directly
    // clocks the sampler and deserializer. PLL clocks the serializer and
    // the outgoing forwarded-clock driver.
    blocks: [
      { id: "rx-pad", label: "RX Bump PAD", short: "RX PAD", topology: null, kind: "pad", col: 0, row: 0 },
      { id: "d2d-rx", label: "RX Receiver / Termination", short: "RX Receiver", topology: "input_termination_buffer", col: 1, row: 0 },
      { id: "d2d-sampler", label: "RX Sampler (Clocked Sense Amp)", short: "Sampler", topology: "sense_amp_slicer", col: 2, row: 0 },
      { id: "d2d-deser", label: "Deserializer", short: "Deserializer", topology: "deserializer", col: 3, row: 0 },
      { id: "clkin-pad", label: "Forwarded Clock In PAD", short: "CLK-IN PAD", topology: null, kind: "pad", col: 0, row: 1 },
      { id: "d2d-clk", label: "Clock RX & Distribution (Delay-matched)", short: "Clock Dist.", topology: "clock_buffer_distribution", col: 1, row: 1 },
      { id: "tx-pad", label: "TX Bump PAD", short: "TX PAD", topology: null, kind: "pad", col: 0, row: 2 },
      { id: "d2d-tx", label: "TX Driver (Impedance-compensated)", short: "TX Driver", topology: "output_driver", col: 1, row: 2 },
      { id: "d2d-ser", label: "Serializer", short: "Serializer", topology: "serializer", col: 2, row: 2 },
      { id: "d2d-pll", label: "PHY PLL (from 100 MHz Ref)", short: "PLL", topology: "pll", col: 3, row: 2 },
      { id: "clkout-pad", label: "Forwarded Clock Out PAD", short: "CLK-OUT PAD", topology: null, kind: "pad", col: 0, row: 3 },
      { id: "d2d-clk-tx", label: "Forwarded Clock TX Driver", short: "CLK Driver", topology: "output_driver", col: 1, row: 3 },
    ],
    edges: [
      { from: "rx-pad", to: "d2d-rx" },
      { from: "d2d-rx", to: "d2d-sampler" },
      { from: "d2d-sampler", to: "d2d-deser" },
      { from: "clkin-pad", to: "d2d-clk" },
      { from: "d2d-clk", to: "d2d-sampler" },
      { from: "d2d-clk", to: "d2d-deser" },
      { from: "d2d-ser", to: "d2d-tx" },
      { from: "d2d-tx", to: "tx-pad" },
      { from: "d2d-pll", to: "d2d-ser" },
      { from: "d2d-pll", to: "d2d-clk-tx" },
      { from: "d2d-clk-tx", to: "clkout-pad" },
    ],
  },
};

// ---------------------------------------------------------------------------
// Hierarchy-aware helpers. A block may carry `children: {blocks, edges}` -
// either statically (defined above) or created by the user merging blocks
// into a group in the diagram. `path` is the list of ancestor block ids from
// the top level down to the level being viewed ([] = top level). All user
// edits (added blocks, hidden blocks, layout, wire edits) are stored per
// (phyType, path) level.

export function levelStorageId(phyType, path = []) {
  return `${phyType}:${path.join("/") || "root"}`;
}

// User-added blocks (from the diagram's "Add block" prompt or "Merge blocks"
// grouping) persist per level. Read fresh on each call so App.jsx's maturity
// computation sees blocks added by the diagram without extra state plumbing.
export function userBlocksKey(phyType, path = []) {
  return `arch-user-blocks-${levelStorageId(phyType, path)}`;
}

export function userBlocksForPhy(phyType, path = []) {
  try {
    const stored = JSON.parse(localStorage.getItem(userBlocksKey(phyType, path)));
    return Array.isArray(stored) ? stored : [];
  } catch {
    return [];
  }
}

// Default blocks the user deleted from the diagram (by id, per level).
// "Reset diagram" clears it. User-added blocks are deleted by removing them
// from the userBlocks list instead.
export function hiddenBlocksKey(phyType, path = []) {
  return `arch-hidden-blocks-${levelStorageId(phyType, path)}`;
}

export function hiddenBlocksForPhy(phyType, path = []) {
  try {
    const stored = JSON.parse(localStorage.getItem(hiddenBlocksKey(phyType, path)));
    return Array.isArray(stored) ? stored : [];
  } catch {
    return [];
  }
}

// The {blocks, edges} definition at a given path, walking children through
// both static blocks and user-created groups.
export function archLevel(phyType, path = []) {
  let level = PHY_ARCHITECTURES[phyType] || { blocks: [], edges: [] };
  for (let i = 0; i < path.length; i++) {
    const merged = [...(level.blocks || []), ...userBlocksForPhy(phyType, path.slice(0, i))];
    const blk = merged.find((b) => b.id === path[i]);
    level = blk?.children || { blocks: [], edges: [] };
  }
  return level;
}

export function blocksForPhy(phyType, path = []) {
  const hidden = hiddenBlocksForPhy(phyType, path);
  return [...(archLevel(phyType, path).blocks || []), ...userBlocksForPhy(phyType, path)].filter(
    (b) => !hidden.includes(b.id)
  );
}

export function edgesForPhy(phyType, path = []) {
  return archLevel(phyType, path).edges || [];
}
