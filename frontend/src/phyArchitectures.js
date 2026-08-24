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
//
// 2026-08-23 (digital-arch spec): the localStorage helper layer is factored
// into makeArchStore(prefix, archMap) so the DIGITAL architecture view
// (frontend/src/digitalArchitectures.js) reuses the exact same
// hierarchy-aware edit records under its own key namespace ("dig-arch-" vs
// "arch-") instead of duplicating them. The named exports below delegate to
// the default "arch-" store, so every pre-existing storage key and call site
// is unchanged.

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
// Namespaced, hierarchy-aware store factory. A block may carry `children:
// {blocks, edges}` - either statically (defined in the arch map) or created
// by the user merging blocks into a group in the diagram. `path` is the list
// of ancestor block ids from the top level down to the level being viewed
// ([] = top level). All user edits (added blocks, hidden blocks, layout,
// wire edits) are stored per (phyType, path) level, under localStorage keys
// namespaced by `prefix` ("arch-" = the AFE view, "dig-arch-" = the digital
// view) so the two diagram workspaces never collide.

function truncateShort(s, n = 16) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function wireKey(c) {
  return `${c.from}->${c.to}`;
}

export function makeArchStore(prefix, archMap) {
  const levelStorageId = (phyType, path = []) => `${phyType}:${path.join("/") || "root"}`;

  // User-added blocks (from the diagram's "Add block" prompt or "Merge
  // blocks" grouping) persist per level. Read fresh on each call so callers'
  // maturity computations see blocks added by the diagram without extra
  // state plumbing.
  const userBlocksKey = (phyType, path = []) =>
    `${prefix}user-blocks-${levelStorageId(phyType, path)}`;

  function userBlocksForPhy(phyType, path = []) {
    try {
      const stored = JSON.parse(localStorage.getItem(userBlocksKey(phyType, path)));
      return Array.isArray(stored) ? stored : [];
    } catch {
      return [];
    }
  }

  // Default blocks the user deleted from the diagram (by id, per level).
  // "Reset diagram" clears it. User-added blocks are deleted by removing
  // them from the userBlocks list instead.
  const hiddenBlocksKey = (phyType, path = []) =>
    `${prefix}hidden-blocks-${levelStorageId(phyType, path)}`;

  function hiddenBlocksForPhy(phyType, path = []) {
    try {
      const stored = JSON.parse(localStorage.getItem(hiddenBlocksKey(phyType, path)));
      return Array.isArray(stored) ? stored : [];
    } catch {
      return [];
    }
  }

  // The {blocks, edges} definition at a given path, walking children through
  // both static blocks and user-created groups.
  function archLevel(phyType, path = []) {
    let level = archMap[phyType] || { blocks: [], edges: [] };
    for (let i = 0; i < path.length; i++) {
      const merged = [...(level.blocks || []), ...userBlocksForPhy(phyType, path.slice(0, i))];
      const blk = merged.find((b) => b.id === path[i]);
      level = blk?.children || { blocks: [], edges: [] };
    }
    return level;
  }

  // Per-block property overrides (label/short/topology) on DEFAULT blocks -
  // used by chat patches (rename_block / set_topology) which may target
  // built-in blocks that don't live in the userBlocks list. Keyed by id.
  const blockOverridesKey = (phyType, path = []) =>
    `${prefix}block-overrides-${levelStorageId(phyType, path)}`;

  function blockOverridesForPhy(phyType, path = []) {
    try {
      const stored = JSON.parse(localStorage.getItem(blockOverridesKey(phyType, path)));
      return stored && typeof stored === "object" && !Array.isArray(stored) ? stored : {};
    } catch {
      return {};
    }
  }

  function blocksForPhy(phyType, path = []) {
    const hidden = hiddenBlocksForPhy(phyType, path);
    const overrides = blockOverridesForPhy(phyType, path);
    return (
      [...(archLevel(phyType, path).blocks || []), ...userBlocksForPhy(phyType, path)]
        // hidden only ever holds DEFAULT block ids - a user/loaded block may
        // legitimately reuse a default id (e.g. a saved architecture keeping
        // "sampler"), so don't let the hidden default swallow it
        .filter((b) => b.user || !hidden.includes(b.id))
        .map((b) => (overrides[b.id] ? { ...b, ...overrides[b.id] } : b))
    );
  }

  function edgesForPhy(phyType, path = []) {
    return archLevel(phyType, path).edges || [];
  }

  const wireEditsKey = (phyType, path = []) =>
    `${prefix}wires-${levelStorageId(phyType, path)}`;

  const layoutKeyForPhy = (phyType, path = []) =>
    `${prefix}layout-${levelStorageId(phyType, path)}`;

  function wireEditsForPhy(phyType, path = []) {
    try {
      const stored = JSON.parse(localStorage.getItem(wireEditsKey(phyType, path)));
      return stored && Array.isArray(stored.removed) && Array.isArray(stored.added)
        ? stored
        : { removed: [], added: [] };
    } catch {
      return { removed: [], added: [] };
    }
  }

  // The wires actually displayed at a level: defaults minus user/patch
  // deletions, plus manual/patch additions, deduped, dangling dropped
  // (removing a block takes its wires with it) - same rule the diagram uses.
  function effectiveEdgesForPhy(phyType, path = []) {
    const edits = wireEditsForPhy(phyType, path);
    const ids = new Set(blocksForPhy(phyType, path).map((b) => b.id));
    const out = [];
    const seen = new Set();
    for (const c of [
      ...edgesForPhy(phyType, path).filter((c) => !edits.removed.includes(wireKey(c))),
      ...edits.added,
    ]) {
      const key = wireKey(c);
      if (seen.has(key) || !ids.has(c.from) || !ids.has(c.to)) continue;
      seen.add(key);
      out.push({ from: c.from, to: c.to });
    }
    return out;
  }

  // The top-level architecture exactly as displayed - the shape POST
  // /api/arch_chat and /api/architectures expect ({blocks, edges}; extra
  // block keys pass through untouched).
  function currentArchitecture(phyType) {
    return { blocks: blocksForPhy(phyType), edges: effectiveEdgesForPhy(phyType) };
  }

  const ARCH_STATE_KEYS = [userBlocksKey, hiddenBlocksKey, blockOverridesKey, wireEditsKey, layoutKeyForPhy];

  // Snapshot/restore of the whole top-level edit record set - Undo for an
  // applied chat patch (and for a loaded architecture).
  function archSnapshot(phyType) {
    const snap = {};
    for (const keyFn of ARCH_STATE_KEYS) {
      const k = keyFn(phyType);
      snap[k] = localStorage.getItem(k);
    }
    return snap;
  }

  function restoreArchSnapshot(phyType, snap) {
    for (const keyFn of ARCH_STATE_KEYS) {
      const k = keyFn(phyType);
      if (snap[k] == null) localStorage.removeItem(k);
      else localStorage.setItem(k, snap[k]);
    }
  }

  // Apply a server-validated chat patch (ops IN ORDER) to the top-level
  // architecture. remove_block also purges its wires; ids/topologies were
  // already validated server-side, so this just executes.
  function applyArchOps(phyType, ops) {
    let user = userBlocksForPhy(phyType);
    let hidden = hiddenBlocksForPhy(phyType);
    let overrides = { ...blockOverridesForPhy(phyType) };
    let edits = { removed: [...wireEditsForPhy(phyType).removed], added: [...wireEditsForPhy(phyType).added] };
    const defaults = archLevel(phyType).blocks || [];
    const defaultEdges = archLevel(phyType).edges || [];

    const visibleBlocks = () =>
      [...defaults, ...user].filter((b) => b.user || !hidden.includes(b.id));

    for (const op of ops || []) {
      switch (op.op) {
        case "add_block": {
          const vis = visibleBlocks();
          const maxRow = Math.max(0, ...vis.map((b) => b.row ?? 0));
          // Consultants sometimes drop a new block onto an already-occupied
          // grid cell (they don't see the layout) - bump it down to the first
          // free row in that column so nothing renders hidden underneath.
          const occupied = new Set(vis.map((b, i) => `${b.col ?? i},${b.row ?? 0}`));
          const col = op.col ?? 0;
          let row = op.row ?? maxRow + 1;
          while (occupied.has(`${col},${row}`)) row += 1;
          user = [
            ...user,
            {
              id: op.id,
              label: op.label,
              short: truncateShort(op.label),
              topology: op.topology ?? null,
              ...(op.kind ? { kind: op.kind } : {}),
              col,
              row,
              user: true,
            },
          ];
          break;
        }
        case "remove_block": {
          if (user.some((b) => b.id === op.id)) {
            user = user.filter((b) => b.id !== op.id);
          } else {
            if (!hidden.includes(op.id)) hidden = [...hidden, op.id];
          }
          delete overrides[op.id];
          // purge wires touching the block (dangling wires are dropped at
          // render time too, but keep the records clean)
          edits.added = edits.added.filter((c) => c.from !== op.id && c.to !== op.id);
          for (const c of defaultEdges) {
            if ((c.from === op.id || c.to === op.id) && !edits.removed.includes(wireKey(c))) {
              edits.removed = [...edits.removed, wireKey(c)];
            }
          }
          break;
        }
        case "rename_block": {
          if (user.some((b) => b.id === op.id)) {
            user = user.map((b) =>
              b.id === op.id ? { ...b, label: op.label, short: truncateShort(op.label) } : b
            );
          } else {
            overrides[op.id] = { ...(overrides[op.id] || {}), label: op.label, short: truncateShort(op.label) };
          }
          break;
        }
        case "set_topology": {
          const topo = op.topology ?? null;
          if (user.some((b) => b.id === op.id)) {
            user = user.map((b) => (b.id === op.id ? { ...b, topology: topo } : b));
          } else {
            overrides[op.id] = { ...(overrides[op.id] || {}), topology: topo };
          }
          break;
        }
        case "add_connection": {
          const key = wireKey(op);
          if (edits.removed.includes(key)) {
            edits.removed = edits.removed.filter((k) => k !== key);
          } else if (
            !edits.added.some((c) => wireKey(c) === key) &&
            !defaultEdges.some((c) => wireKey(c) === key)
          ) {
            edits.added = [...edits.added, { from: op.from, to: op.to }];
          }
          break;
        }
        case "remove_connection": {
          const key = wireKey(op);
          if (edits.added.some((c) => wireKey(c) === key)) {
            edits.added = edits.added.filter((c) => wireKey(c) !== key);
          } else if (!edits.removed.includes(key)) {
            edits.removed = [...edits.removed, key];
          }
          break;
        }
        default:
          break; // unknown ops never reach here (server rejects them)
      }
    }

    try {
      localStorage.setItem(userBlocksKey(phyType), JSON.stringify(user));
      localStorage.setItem(hiddenBlocksKey(phyType), JSON.stringify(hidden));
      localStorage.setItem(blockOverridesKey(phyType), JSON.stringify(overrides));
      localStorage.setItem(wireEditsKey(phyType), JSON.stringify(edits));
    } catch {
      // localStorage unavailable - the patch just won't persist
    }
  }

  // Replace the displayed top-level architecture of `phyType` with a saved
  // custom one: every saved block becomes a user block (defaults all hidden),
  // every saved edge a manual wire. "Reset diagram" still restores defaults.
  function loadArchitectureIntoPhy(phyType, arch) {
    const blocks = (arch?.blocks || []).map((b) => ({ ...b, user: true }));
    const defaults = archLevel(phyType).blocks || [];
    const defaultEdges = archLevel(phyType).edges || [];
    try {
      localStorage.setItem(userBlocksKey(phyType), JSON.stringify(blocks));
      localStorage.setItem(hiddenBlocksKey(phyType), JSON.stringify(defaults.map((b) => b.id)));
      localStorage.setItem(blockOverridesKey(phyType), JSON.stringify({}));
      localStorage.setItem(
        wireEditsKey(phyType),
        JSON.stringify({ removed: defaultEdges.map(wireKey), added: arch?.edges || [] })
      );
      localStorage.removeItem(layoutKeyForPhy(phyType));
    } catch {
      // localStorage unavailable
    }
  }

  return {
    prefix,
    archMap,
    levelStorageId,
    userBlocksKey,
    userBlocksForPhy,
    hiddenBlocksKey,
    hiddenBlocksForPhy,
    archLevel,
    blockOverridesKey,
    blockOverridesForPhy,
    blocksForPhy,
    edgesForPhy,
    wireEditsKey,
    layoutKeyForPhy,
    wireEditsForPhy,
    effectiveEdgesForPhy,
    currentArchitecture,
    archSnapshot,
    restoreArchSnapshot,
    applyArchOps,
    loadArchitectureIntoPhy,
  };
}

// The default (AFE) store: exact same storage keys as before the factoring
// ("arch-user-blocks-...", "arch-wires-...", ...), so existing user edits
// survive this refactor untouched.
export const afeArchStore = makeArchStore("arch-", PHY_ARCHITECTURES);

// Backwards-compatible named exports (every pre-existing call site).
export const levelStorageId = afeArchStore.levelStorageId;
export const userBlocksKey = afeArchStore.userBlocksKey;
export const userBlocksForPhy = afeArchStore.userBlocksForPhy;
export const hiddenBlocksKey = afeArchStore.hiddenBlocksKey;
export const hiddenBlocksForPhy = afeArchStore.hiddenBlocksForPhy;
export const archLevel = afeArchStore.archLevel;
export const blockOverridesKey = afeArchStore.blockOverridesKey;
export const blockOverridesForPhy = afeArchStore.blockOverridesForPhy;
export const blocksForPhy = afeArchStore.blocksForPhy;
export const edgesForPhy = afeArchStore.edgesForPhy;
export const wireEditsKey = afeArchStore.wireEditsKey;
export const layoutKeyForPhy = afeArchStore.layoutKeyForPhy;
export const wireEditsForPhy = afeArchStore.wireEditsForPhy;
export const effectiveEdgesForPhy = afeArchStore.effectiveEdgesForPhy;
export const currentArchitecture = afeArchStore.currentArchitecture;
export const archSnapshot = afeArchStore.archSnapshot;
export const restoreArchSnapshot = afeArchStore.restoreArchSnapshot;
export const applyArchOps = afeArchStore.applyArchOps;
export const loadArchitectureIntoPhy = afeArchStore.loadArchitectureIntoPhy;
