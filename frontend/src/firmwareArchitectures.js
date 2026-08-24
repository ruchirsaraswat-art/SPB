// Firmware-architecture diagrams (2026-08-23 firmware-section spec, section
// b): the built-in firmware stack per PHY type, in EXACTLY the
// PHY_ARCHITECTURES data shape. `topology` values reference the FIRMWARE
// block catalog (GET /api/firmware_blocks); `topology: null` + `kind:
// "iface"` marks the block's two contracts as boundary anchors: `host-if`
// (up - the host/SoC API surface, stub-only in the v1 codegen) and `csr-if`
// (down - the rendered stand-in for the stored interface definition, i.e.
// the controller CSRs firmware drives through the driver layer).
//
// Layering convention (all diagrams): row 0 = host boundary, row 1 = API
// layer, row 2 = supervisors/services, row 3 = driver + IRQ plumbing,
// row 4 = CSR boundary. An edge INTO `drv` means "this component touches
// CSRs only through the driver layer" - no supervisor talks to `csr-if`
// directly (the fw chat flags such an edge as a layering violation).
//
// The localStorage edit-record layer is the SAME hierarchy-aware store as
// the AFE/digital views, namespaced "fw-arch-" (makeArchStore in
// phyArchitectures.js) - fw_chat patches apply through
// firmwareArchStore.applyArchOps and the renderer reads the store.
// Rendering accent (variant="firmware") is Graphics_Dev's slice - see the
// handoff appended to audits/2026-08-23-firmware-section-spec.md.

import { makeArchStore } from "./phyArchitectures";

export const FIRMWARE_DESCRIPTIONS = {
  "ser-des": "Bare-metal C control plane: boot/power sequencing, link-training + cal supervisors, eye sweeps, diagnostics - all through the register driver",
  "ddr": "JEDEC init, DDR training supervision, ZQ recal and frequency switching over the controller CSRs (reuses the LPDDR firmware stack in v1)",
  "lpddr": "JEDEC init, DDR training supervision, ZQ recal and frequency switching over the controller CSRs",
  "hbm": "LPDDR firmware stack (add lane-repair service when needed; reuses the LPDDR diagram in v1)",
  "optical": "TIA/LA calibration supervision, LOS-aware link policy, eye sweeps and diagnostics over the controller CSRs",
  "die-to-die": "Link bringup driving the LTSM, sideband messaging, lane repair and diagnostics over the controller CSRs",
};

export const FIRMWARE_ARCHITECTURES = {
  "ser-des": {
    blocks: [
      { id: "host-if", label: "Host / SoC API (C calls)", short: "HOST API IF", topology: null, kind: "iface", col: 2, row: 0 },
      { id: "api", label: "Host API Service Layer", short: "API", topology: "host_api_service", col: 2, row: 1 },
      { id: "boot", label: "Boot / Init Sequencer", short: "Boot Seq", topology: "boot_init_sequencer", col: 0, row: 2 },
      { id: "pwr", label: "Power-State Manager", short: "Pwr Mgr", topology: "power_state_manager", col: 1, row: 2 },
      { id: "train", label: "Link-Training Supervisor", short: "Train Sup", topology: "training_supervisor", col: 2, row: 2 },
      { id: "cal", label: "Calibration Supervisor", short: "Cal Sup", topology: "cal_supervisor", col: 3, row: 2 },
      { id: "eye", label: "Eye-Monitor Sweep Service", short: "Eye Sweep", topology: "eye_sweep_service", col: 4, row: 2 },
      { id: "diag", label: "Diagnostics / BIST Orchestrator", short: "Diag/BIST", topology: "diag_bist_orchestrator", col: 5, row: 2 },
      { id: "irq", label: "IRQ / Event Handler", short: "IRQ", topology: "irq_event_handler", col: 0, row: 3 },
      { id: "drv", label: "Register-Access Driver (HAL)", short: "Reg Drv", topology: "reg_access_driver", col: 2, row: 3 },
      { id: "csr-if", label: "Controller CSRs (APB, per interface definition)", short: "CSR IF", topology: null, kind: "iface", col: 2, row: 4 },
    ],
    edges: [
      { from: "host-if", to: "api" },
      { from: "api", to: "boot" }, { from: "api", to: "pwr" },
      { from: "api", to: "train" }, { from: "api", to: "diag" },
      { from: "api", to: "eye" },
      { from: "boot", to: "train" },          // boot hands off to training
      { from: "eye", to: "cal" },             // eye data informs trim policy
      { from: "boot", to: "drv" }, { from: "pwr", to: "drv" },
      { from: "train", to: "drv" }, { from: "cal", to: "drv" },
      { from: "eye", to: "drv" }, { from: "diag", to: "drv" },
      { from: "drv", to: "csr-if" },
      { from: "csr-if", to: "irq" },          // IRQ line / status events up
      { from: "irq", to: "train" }, { from: "irq", to: "pwr" },
      { from: "irq", to: "api" },             // event callbacks to host
    ],
  },
  "lpddr": {
    blocks: [
      { id: "host-if", label: "Host / SoC API (C calls)", short: "HOST API IF", topology: null, kind: "iface", col: 2, row: 0 },
      { id: "api", label: "Host API Service Layer", short: "API", topology: "host_api_service", col: 2, row: 1 },
      { id: "boot", label: "Boot / Init Sequencer (JEDEC init)", short: "Boot Seq", topology: "boot_init_sequencer", col: 0, row: 2 },
      { id: "pwr", label: "Power-State Manager", short: "Pwr Mgr", topology: "power_state_manager", col: 1, row: 2 },
      { id: "train", label: "DDR Training Supervisor (leveling/deskew/Vref)", short: "Train Sup", topology: "training_supervisor", col: 2, row: 2 },
      { id: "freq", label: "Frequency-Switch Manager", short: "Freq Sw", topology: "freq_switch_manager", col: 3, row: 2 },
      { id: "zq", label: "ZQ Recal Service", short: "ZQ Recal", topology: "zq_recal_service", col: 4, row: 2 },
      { id: "diag", label: "Diagnostics / BIST Orchestrator", short: "Diag/BIST", topology: "diag_bist_orchestrator", col: 5, row: 2 },
      { id: "irq", label: "IRQ / Event Handler", short: "IRQ", topology: "irq_event_handler", col: 0, row: 3 },
      { id: "drv", label: "Register-Access Driver (HAL)", short: "Reg Drv", topology: "reg_access_driver", col: 2, row: 3 },
      { id: "csr-if", label: "Controller CSRs (APB, per interface definition)", short: "CSR IF", topology: null, kind: "iface", col: 2, row: 4 },
    ],
    edges: [
      { from: "host-if", to: "api" },
      { from: "api", to: "boot" }, { from: "api", to: "pwr" },
      { from: "api", to: "freq" }, { from: "api", to: "diag" },
      { from: "boot", to: "train" },
      { from: "freq", to: "train" },          // switch triggers (re)train
      { from: "boot", to: "drv" }, { from: "pwr", to: "drv" },
      { from: "train", to: "drv" }, { from: "freq", to: "drv" },
      { from: "zq", to: "drv" }, { from: "diag", to: "drv" },
      { from: "drv", to: "csr-if" },
      { from: "csr-if", to: "irq" },
      { from: "irq", to: "zq" },              // ZQ timer/req events
      { from: "irq", to: "api" },
    ],
  },
  "die-to-die": {
    blocks: [
      { id: "host-if", label: "Host / SoC API (C calls)", short: "HOST API IF", topology: null, kind: "iface", col: 2, row: 0 },
      { id: "api", label: "Host API Service Layer", short: "API", topology: "host_api_service", col: 2, row: 1 },
      { id: "boot", label: "Boot / Init Sequencer", short: "Boot Seq", topology: "boot_init_sequencer", col: 0, row: 2 },
      { id: "pwr", label: "Power-State Manager", short: "Pwr Mgr", topology: "power_state_manager", col: 1, row: 2 },
      { id: "train", label: "Link-Bringup Supervisor (drives LTSM)", short: "Train Sup", topology: "training_supervisor", col: 2, row: 2 },
      { id: "sb", label: "Sideband Message Service", short: "Sideband Svc", topology: "sideband_msg_service", col: 3, row: 2 },
      { id: "repair", label: "Lane Repair Service", short: "Lane Repair", topology: "lane_repair_service", col: 4, row: 2 },
      { id: "diag", label: "Diagnostics / BIST Orchestrator", short: "Diag/BIST", topology: "diag_bist_orchestrator", col: 5, row: 2 },
      { id: "irq", label: "IRQ / Event Handler", short: "IRQ", topology: "irq_event_handler", col: 0, row: 3 },
      { id: "drv", label: "Register-Access Driver (HAL)", short: "Reg Drv", topology: "reg_access_driver", col: 2, row: 3 },
      { id: "csr-if", label: "Controller CSRs (APB, per interface definition)", short: "CSR IF", topology: null, kind: "iface", col: 2, row: 4 },
    ],
    edges: [
      { from: "host-if", to: "api" },
      { from: "api", to: "boot" }, { from: "api", to: "pwr" },
      { from: "api", to: "diag" },
      { from: "boot", to: "train" },
      { from: "train", to: "sb" },            // param exchange during bringup
      { from: "diag", to: "repair" },         // BIST map feeds repair
      { from: "boot", to: "drv" }, { from: "pwr", to: "drv" },
      { from: "train", to: "drv" }, { from: "sb", to: "drv" },
      { from: "repair", to: "drv" }, { from: "diag", to: "drv" },
      { from: "drv", to: "csr-if" },
      { from: "csr-if", to: "irq" },
      { from: "irq", to: "train" }, { from: "irq", to: "sb" },
      { from: "irq", to: "api" },
    ],
  },
  "optical": {
    blocks: [
      { id: "host-if", label: "Host / SoC API (C calls)", short: "HOST API IF", topology: null, kind: "iface", col: 2, row: 0 },
      { id: "api", label: "Host API Service Layer", short: "API", topology: "host_api_service", col: 2, row: 1 },
      { id: "boot", label: "Boot / Init Sequencer", short: "Boot Seq", topology: "boot_init_sequencer", col: 0, row: 2 },
      { id: "pwr", label: "Power-State Manager", short: "Pwr Mgr", topology: "power_state_manager", col: 1, row: 2 },
      { id: "cal", label: "TIA/LA Calibration Supervisor", short: "Cal Sup", topology: "cal_supervisor", col: 2, row: 2 },
      { id: "train", label: "Link Supervisor (incl. LOS policy)", short: "Link Sup", topology: "training_supervisor", col: 3, row: 2 },
      { id: "eye", label: "Eye-Monitor Sweep Service", short: "Eye Sweep", topology: "eye_sweep_service", col: 4, row: 2 },
      { id: "diag", label: "Diagnostics / BIST Orchestrator", short: "Diag/BIST", topology: "diag_bist_orchestrator", col: 5, row: 2 },
      { id: "irq", label: "IRQ / Event Handler", short: "IRQ", topology: "irq_event_handler", col: 0, row: 3 },
      { id: "drv", label: "Register-Access Driver (HAL)", short: "Reg Drv", topology: "reg_access_driver", col: 2, row: 3 },
      { id: "csr-if", label: "Controller CSRs (APB, per interface definition)", short: "CSR IF", topology: null, kind: "iface", col: 2, row: 4 },
    ],
    edges: [
      { from: "host-if", to: "api" },
      { from: "api", to: "boot" }, { from: "api", to: "pwr" },
      { from: "api", to: "diag" }, { from: "api", to: "eye" },
      { from: "boot", to: "cal" },            // offset cal before link up
      { from: "cal", to: "train" },
      { from: "boot", to: "drv" }, { from: "pwr", to: "drv" },
      { from: "cal", to: "drv" }, { from: "train", to: "drv" },
      { from: "eye", to: "drv" }, { from: "diag", to: "drv" },
      { from: "drv", to: "csr-if" },
      { from: "csr-if", to: "irq" },          // LOS / lock-loss events
      { from: "irq", to: "train" }, { from: "irq", to: "api" },
    ],
  },
};

// "ddr" and "hbm": v1 reuses the "lpddr" firmware stack (hbm: add
// lane_repair_service when someone asks). Shallow reuse is fine - user
// edits are stored per phyType key ("fw-arch-...-ddr:..." etc.), not on the
// shared object.
FIRMWARE_ARCHITECTURES.ddr = FIRMWARE_ARCHITECTURES.lpddr;
FIRMWARE_ARCHITECTURES.hbm = FIRMWARE_ARCHITECTURES.lpddr;

// The firmware view's edit-record store: same helper layer as the AFE and
// digital views, separate "fw-arch-" localStorage namespace. This is what
// the firmware chat applies patches through and what the renderer reads.
export const firmwareArchStore = makeArchStore("fw-arch-", FIRMWARE_ARCHITECTURES);
