# 2026-08-23 - Firmware section + Firmware overview block: feature spec

Author: Digital_Microarchitect. Companion to
`audits/2026-08-23-digital-arch-spec.md` - ALL conventions carry over
unchanged (catalog schema = `TOPOLOGY_FIELDS` style; diagram data shape =
`PHY_ARCHITECTURES` style with `makeArchStore` namespacing; chat/patch
contract = 2026-08-21 arch-chat contract; stacked minimizable panels with
focus-following chat). Persona behind the firmware chat and codegen:
**Firmware_Coder** (`~/.claude/agents/Firmware_Coder.md`) - C99, no dynamic
allocation, `reg_read/reg_write` port layer, interface JSON under
`<working_dir>/interfaces/` is the source of truth, host-compiled unit tests
with `gcc -std=c99 -Wall -Werror`.

Research notes: `~/.claude/agent-knowledge/digital-microarch/`
(INDEX.md -> firmware-stack-for-phy-bringup.md).

---

## (a) Firmware block catalog

New file `backend/firmware_blocks.py`, served by `GET /api/firmware_blocks`
-> `{groups, by_phy}`, exact same shape as `/api/digital_blocks` with ONE
deliberate difference:

**NO `models` key on any option.** Firmware is C code compiled with gcc, not
Verilog - the veriloga/verilog/rnm behavioral-model levels do not apply and
must not be offered. The frontend keys the model-checkbox UI off the
presence of `models`; firmware options omit it entirely, so the models UI
stays off with zero special-casing. (The firmware equivalent of "model
verification" is the codegen contract in (d): gcc compile + host unit test.)
Add a top-level `"note"` string in the payload for honesty in the API:
`"Firmware blocks have no behavioral-model levels: firmware is host-compiled
C (gcc), verified by compile + unit test, not by iverilog."`

Separate namespace: nothing added to `TOPOLOGY_GROUPS` or the digital
catalog (assert by test, same as digital). Firmware diagram `topology`
strings validate against this catalog only.

### Catalog (value / label / fields)

Field tuple style identical to `TOPOLOGY_FIELDS`: `name` ("Label", "unit",
constraints) - help text.

Group **"Boot & power"**:

- `boot_init_sequencer` - "Boot / init sequencer"
  - `boot_time_budget_ms` ("Boot-time budget", "ms", min 0) - help: "Wall-clock from reset release to link ACTIVE. Floor = sum of the interface power_sequence waits (e.g. SerDes default: ~1.5 ms of settle/lock timeouts); budget must exceed it."
  - `num_init_steps` ("Init step count (estimate)", "", min 1) - help: "Entries in the init table; should track the interface reset_sequence + power_sequence step counts."
  - `poll_interval_us` ("Status poll interval", "us", min 1) - help: "Polling cadence for wait_for status bits (pll_locked, cdr_locked) when no IRQ is wired."
  - `on_timeout` ("Timeout policy", "", text) - help: "Per-step give-up behavior: 'retry N then fail', 'fail-stop', 'degrade (skip optional step)'. Mirrors the retry annotations in the interface power_sequence."
  - `config_source` ("Config source", "", text) - help: "'compiled-in defaults' or 'host-supplied config table' - decides whether trim seeds live in the binary or come from the SoC."
- `power_state_manager` - "Power-state manager"
  - `num_power_states` ("Power state count", "", min 2) - help: "Mirrors the controller power_state_fsm states (e.g. OFF/SLEEP/STANDBY/ACTIVE); firmware sequences entry/exit via CSRs."
  - `wake_latency_budget_us` ("Wake latency budget", "us", min 0) - help: "Deepest-sleep to ACTIVE; must cover PLL relock + CDR relock per the interface sequence timeouts."
  - `entry_policy` ("Sleep entry policy", "", text) - help: "'host-commanded only' or 'inactivity timer'."
  - `inactivity_timeout_ms` ("Inactivity timeout", "ms", min 0) - help: "0 = no autonomous sleep entry."

Group **"Register access & events"**:

- `reg_access_driver` - "Register-access driver layer (HAL)"
  - `bus_protocol` ("Host bus protocol", "", text) - help: "Must match the controller csr_regfile bus_protocol (APB3 for v1). Documentation field - the driver only sees MMIO."
  - `data_width_bits` ("Access width", "bits", min 8, max 64) - help: "CSR access width; 32 recommended (one signal per 32-bit register in v1 codegen)."
  - `base_addr` ("MMIO base address", "", text) - help: "Hex base of the PHY CSR window in the SoC map, e.g. 0x4001_0000. Codegen emits it as a single #define."
  - `poll_timeout_us` ("Default poll timeout", "us", min 0) - help: "Give-up default for read-poll helpers (wait-for-bit); per-call override allowed."
- `irq_event_handler` - "IRQ / event handler"
  - `num_irq_sources` ("IRQ sources", "", min 1) - help: "Status events routed as interrupts (lock-loss, training-done, cal-done, CRC error...). Each must be an a2d status signal in the interface table."
  - `irq_latency_budget_us` ("IRQ latency budget", "us", min 0) - help: "Worst-case assert-to-handler-CSR-access; drives whether polling is acceptable instead."
  - `dispatch_model` ("Dispatch model", "", text) - help: "'polled loop', 'vectored ISR', or 'mailbox to host' - v1 codegen only stubs a polled dispatcher."
  - `event_queue_depth` ("Event queue depth", "events", min 1) - help: "Deferred-work queue between ISR context and supervisors."

Group **"Calibration & training"**:

- `cal_supervisor` - "Calibration supervisor"
  - `cal_interval_ms` ("Recalibration interval", "ms", min 0) - help: "0 = boot-only. Periodic recal tracks V/T drift; must match/drive the controller cal_engine and zq_cal_fsm interval fields."
  - `cal_time_budget_us` ("Per-cal time budget", "us", min 0) - help: "Ceiling per cal pass so periodic recal never stalls traffic beyond this."
  - `num_cal_targets` ("Cal targets managed", "", min 1) - help: "Trim buses this supervisor owns; each must be a d2a quasi_static signal in the interface table (ctle_peak, vga_gain, slicer_ofst...)."
  - `drift_trigger` ("Off-schedule trigger", "", text) - help: "What forces recal outside the interval: 'temp-code delta', 'error-count threshold', 'none'."
  - `retry_limit` ("Cal retry limit", "", min 0) - help: "Failed-cal retries before raising an event to the host."
- `training_supervisor` - "Link / DDR training supervisor"
  - `training_timeout_ms` ("Training timeout", "ms", min 0) - help: "Firmware-level give-up; must exceed the controller training FSM's own training_timeout_ms."
  - `max_retrain_attempts` ("Max retrain attempts", "", min 0) - help: "Autonomous retrains before escalating to the host."
  - `retrain_trigger` ("Retrain trigger", "", text) - help: "'lock loss (cdr_locked/sig_detect)', 'CRC error rate', 'host command', 'frequency switch'."
  - `adapt_targets` ("Adaptation targets", "", text) - help: "Mirrors the controller link_training_fsm.adapt_targets / ddr_training_engine.trainings; firmware owns policy + sequencing, hardware owns the inner loop."
- `eye_sweep_service` - "Eye-monitor sweep service"
  - `phase_steps` ("Phase steps across UI", "", min 2) - help: "Must equal the controller eye_monitor_ctrl.phase_steps (and the eye_phase_code width in the interface table)."
  - `vref_steps` ("Vref steps", "", min 2) - help: "Must equal eye_monitor_ctrl.vref_steps / eye_vref_code range."
  - `samples_per_point` ("Samples per point", "", min 1) - help: "Bounds the measurable BER floor per point (~1/samples)."
  - `sweep_time_budget_ms` ("Full-sweep budget", "ms", min 0) - help: "Sanity: >= phase_steps * vref_steps * samples_per_point / word_rate."

Group **"Diagnostics & host"**:

- `diag_bist_orchestrator` - "Diagnostics / BIST orchestrator"
  - `prbs_order` ("PRBS order", "", min 7, max 31) - help: "Must match the controller prbs_gen_checker.prbs_order."
  - `dwell_time_ms` ("Per-test dwell", "ms", min 0) - help: "PRBS run length per measurement; with the word rate this sets the confidence floor of the BER estimate."
  - `error_threshold` ("Fail threshold", "errors", min 0) - help: "Errors within the dwell that mark the test failed."
  - `report_format` ("Report format", "", text) - help: "'pass/fail bitmap', 'per-lane error counts', 'JSON blob via host API' - documentation in v1."
- `host_api_service` - "Host / SoC API layer"
  - `api_style` ("API style", "", text) - help: "'blocking C calls' (v1 codegen target), 'async + callbacks', 'mailbox/doorbell'."
  - `num_api_entry_points` ("API entry points (estimate)", "", min 1) - help: "init/up/down/sleep/wake/status/diag... - the surface the SoC application sees."
  - `error_model` ("Error model", "", text) - help: "'negative return codes' (v1), 'errno-style', 'status struct'."

Group **"Memory-PHY services (LPDDR/DDR/HBM)"**:

- `zq_recal_service` - "ZQ periodic recalibration service"
  - `recal_interval_ms` ("Recal interval", "ms", min 0) - help: "Drives the controller zq_cal_fsm.cal_interval_ms; 0 = boot-only."
  - `zq_timeout_us` ("ZQ cal timeout", "us", min 0)
  - `on_fail` ("Failure policy", "", text) - help: "'keep last code + event', 'retry', 'fail link'."
- `freq_switch_manager` - "Frequency-switch manager"
  - `num_operating_points` ("Operating points", "", min 1) - help: "Frequency set points (e.g. boot 50 MHz, mission 100/200 MHz dfi_clk)."
  - `switch_time_budget_us` ("Switch time budget", "us", min 0) - help: "Traffic-stopped window: PLL re-lock + retrain per the interface sequences."
  - `retrain_on_switch` ("Retrain on switch", "", text) - help: "'full', 'shortened (saved-code restore + verify)', 'none'."

Group **"Die-to-die services (UCIe-style)"**:

- `sideband_msg_service` - "Sideband message service"
  - `msg_timeout_ms` ("Message timeout", "ms", min 0) - help: "Per-request timeout on the sideband mailbox (parameter exchange, remote CSR access)."
  - `num_msg_types` ("Message types (estimate)", "", min 1)
  - `param_exchange_policy` ("Param exchange policy", "", text) - help: "'fixed table' or 'negotiated (advertise + resolve)' during SBINIT/PARAM."
- `lane_repair_service` - "Lane repair service"
  - `repair_trigger` ("Repair trigger", "", text) - help: "'boot BIST map only' or 'runtime per-lane error escalation'."
  - `max_repairs` ("Max repairs", "lanes", min 0) - help: "Bounded by the controller lane_repair_mux.num_spare_lanes."

### Per-PHY applicability map

`FIRMWARE_BLOCKS_BY_PHY` (exported with the catalog; fw_chat soft-warns on
off-map `add_block`, never rejects - same rule as digital):

- `ser-des`: boot_init_sequencer, power_state_manager, reg_access_driver, irq_event_handler, cal_supervisor, training_supervisor, eye_sweep_service, diag_bist_orchestrator, host_api_service
- `lpddr` / `ddr`: boot_init_sequencer, power_state_manager, reg_access_driver, irq_event_handler, training_supervisor, zq_recal_service, freq_switch_manager, diag_bist_orchestrator, host_api_service
- `hbm`: lpddr set + lane_repair_service
- `die-to-die`: boot_init_sequencer, power_state_manager, reg_access_driver, irq_event_handler, training_supervisor, sideband_msg_service, lane_repair_service, diag_bist_orchestrator, host_api_service
- `optical`: boot_init_sequencer, power_state_manager, reg_access_driver, irq_event_handler, cal_supervisor, training_supervisor, eye_sweep_service, diag_bist_orchestrator, host_api_service

---

## (b) Firmware diagrams per PHY type

New file `frontend/src/firmwareArchitectures.js`: `FIRMWARE_ARCHITECTURES`
in the exact `PHY_ARCHITECTURES` shape, plus
`firmwareArchStore = makeArchStore("fw-arch-", FIRMWARE_ARCHITECTURES)`.
`ddr`/`hbm` alias `lpddr` (same shallow-reuse rule as digital).
`ArchitectureDiagram` gains `variant="firmware"` (amber/orange accent
suggested to distinguish from teal digital; Graphics_Dev's call), same
`store`/`topologyGroups` props, `kind:"iface"` nodes exactly as in the
digital view. Block clicks are visual-only (nothing feeds the spec form or
any build flow). No model captions/legend (no models at all here).

Layering convention (all four diagrams): row 0 = host boundary, row 1 =
API layer, row 2 = supervisors/services, row 3 = driver + IRQ plumbing,
row 4 = CSR boundary. The two `kind:"iface"` anchors are the block's two
contracts: **up** to the host/SoC API, **down** to the Controller CSRs as
defined by the stored interface definition.

```js
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
// FIRMWARE_ARCHITECTURES.ddr = FIRMWARE_ARCHITECTURES.hbm = FIRMWARE_ARCHITECTURES.lpddr
// (hbm: add lane_repair_service when someone asks; edits store per phyType key).
```

Semantics the fw_chat prompt must state (they are the diagram's meaning):
an edge INTO `drv` = "this component touches CSRs only through the driver
layer" (no supervisor talks to `csr-if` directly - the consultant should
flag such an edge as a layering violation, soft warning); `csr-if` is the
rendered stand-in for the stored interface definition of the current PHY;
`host-if` is the API surface the v1 codegen does NOT generate (stub only).

---

## (c) Overview update: Firmware block in the top PHY depiction

`PhyOverviewPanel.jsx` chain becomes:

```
[Line/channel] <-> AFE <-> IF <-> Controller <-> Firmware <-> [SoC core/host]
```

Placement CONFIRMED, with one precision. The proposed position is correct
for the control plane, which is what this chain models end-to-end: the
firmware is the bus master on the CSR/APB side and the only agent the SoC
application uses to operate the PHY - everything to its left is hardware it
drives. The one real alternative (Firmware as a block hanging BELOW
Controller, off-chain) was rejected: it breaks the established
click-to-focus reading order (panels are stacked in exactly this chain
order) and buries the point that the SoC reaches the PHY through firmware.
Two required renderings to keep the depiction honest:

1. Style Firmware as SOFTWARE, not another hardware stage: distinct fill +
   dashed border + a small "SW" tag (Graphics_Dev). It is not a pipeline
   stage the data flows through.
2. One-line caption under the chain (or on the Firmware block hover):
   "Control plane: SoC operates the PHY through firmware. Data path is
   Controller <-> SoC direct (core interface)." A thin secondary
   Controller<->SoC edge labeled "data" is optional - only if it costs
   nothing visually; the caption alone is acceptable for v1.

Behavior: identical to the AFE/IF/Controller blocks - clicking Firmware
focuses the firmware workspace panel (accent border, chat follows). The
overview stays static + navigational (no state of its own).

---

## (d) Firmware workspace v1

Fifth stacked full-width panel, bottom of the workbench (matching the chain
order): `PHY architecture -> PHY/AFE -> AFE<->Controller interface ->
PHY/Controller -> **PHY/Firmware**`. Individually minimizable, header-click
focuses, chat follows focus - all existing conventions. Panel body:

- The firmware diagram: `ArchitectureDiagram` with `store=firmwareArchStore`,
  `variant="firmware"`, `topologyGroups` from `getFirmwareBlocks()`
  (`by_phy[phyType]` set sorted first, rest offered - same as digital).
  NO model checkboxes anywhere (no `models` in the catalog - see (a)).
- A "Generate register layer" action button (the one codegen action, below)
  with a cost-bearing confirm, same visual contract as other paid runs.

### fw_chat

New `backend/fw_chat.py` + `POST /api/fw_chat` / `GET /api/fw_chat/{session}`
- same envelope, session-id rules, sync POST, timeout, 422/502 semantics,
transcript persistence as if_chat (new `fw_chats/` root in
`backend/settings.py`). A separate endpoint rather than another `arch_chat`
view because the context payload and persona differ materially.

Request body:

```json
{
  "session_id": "fwchat-abc123",
  "message": "Should ZQ recal run from the IRQ handler or a timer poll?",
  "firmware_architecture": {"blocks": [...], "edges": [...]},
  "interface": { ...full stored interface definition, READ-ONLY context... },
  "phy_type": "ser-des"
}
```

(The controller architecture is deliberately NOT sent - the interface
definition already encodes everything firmware can reach; keeping the
payload lean is a token/cost decision.)

Patch ops: exactly the arch_chat diagram op set (add_block / remove_block /
update_block / add_edge / remove_edge / move_block - whatever
`applyArchOps` supports today), validated against the FIRMWARE catalog,
`kind:"iface"` allowed, off-per-PHY topologies soft-warned via
`patch_warnings`. Patch can ONLY touch the firmware diagram; the interface
is read-only (the consultant may recommend interface changes in prose and
direct the user to the interface panel). Frontend: session id
`fw-chat-session-<phy>`, apply/undo through `firmwareArchStore`
snapshot/undo, one undo slot, same as the other workspaces.

Persona prompt = Firmware_Coder charter framing, stated explicitly:
embedded-firmware engineer for this PHY; C99, no dynamic allocation, no OS
assumptions; all hardware access through a `reg_read/reg_write` port layer;
the supplied interface definition is the source of truth - NEVER invent
register fields, report missing ones as findings. Review duties: every
supervisor's targets (adapt_targets, cal targets, IRQ sources) must exist
as signals in the interface definition; timing fields (budgets, timeouts)
must be consistent with the interface reset/power sequence waits; every
CSR-touching block must reach `csr-if` only via `drv` (layering); polled vs
IRQ dispatch must be consistent with which status signals exist.

### The one v1 generation action: register layer + driver/test skeletons

Recommended and IN for v1 - it is cheap (single headless Firmware_Coder
invocation, same paid-run machinery as research/build runs), high-value
(the stored interface JSON becomes running, tested C the day it is defined),
and its honesty check is fully mechanical.

`POST /api/fw_generate`:

```json
{ "phy_type": "ser-des", "interface": { ...definition as the editor holds it... } }
```

Output directory: `<working_dir>/firmware/<phy_type>/` (per the
Firmware_Coder charter). With `<if>` = the interface `name` with `-`
mapped to `_` (e.g. `serdes_rx_if`), deliverables:

| file | content |
|---|---|
| `<if>_regs.h` | Generated register map: `#define <IF>_BASE_ADDR`, per-register `_OFFSET`, per-field `_MASK`/`_SHIFT`/`_WIDTH`, RO/RW access tag in a comment, traceable 1:1 to interface signal names. |
| `<if>_drv.h` / `<if>_drv.c` | `extern uint32_t fw_reg_read32(uint32_t addr); extern void fw_reg_write32(uint32_t addr, uint32_t v);` port layer; per-register get/set accessors; `int <if>_power_up(const <if>_timing_t *t)` / `<if>_power_down()` generated from the interface `power_sequence`/`reset_sequence` (poll `wait_for` status fields with timeout params - NO hard-coded waits: all times in a `<if>_timing_t` struct seeded from the sequence values). |
| `reg_stub.h` / `reg_stub.c` | Host stub register model: array-backed, RO-write-ignored, status-bit injection hooks, access-order log for sequence tests. |
| `test_<if>.c` | Host unit tests: offset uniqueness + 4-byte alignment; field mask/shift round-trip per signal; RO write ignored; `power_up` CSR access order matches the sequence step order (via the stub log); timeout path returns the error code when a status bit is never injected. |
| `Makefile` | `test` target: `gcc -std=c99 -Wall -Werror` compile + run `./test_<if>`. |

Deterministic register-map rule (part of this spec so the generator and the
tests agree; packing multiple fields per register is explicitly v2):

- Include interface signals with `group` in {control, status, reset, power}
  and `direction` in {d2a, a2d}. Exclude `direction:"ext"`, `group:"clock"`,
  and `group:"data"` word-rate buses (not CSR-reachable).
- Order: control, status, reset, power; table order within a group.
- One 32-bit register per signal; `offset = 4 * index`; field at bits
  `[width-1:0]`.
- `d2a` -> RW (readable back), `a2d` -> RO (stub ignores writes).
- Names: `<IF>_<SIGNAL>` uppercased (e.g. `SERDES_RX_IF_CTLE_PEAK_OFFSET`).

Deliverable/verification contract (the honesty check): the backend runs
`make -C <working_dir>/firmware/<phy_type> test` after the agent reports
done. `status:"pass"` ONLY if gcc exits 0 under `-std=c99 -Wall -Werror`
AND the test binary exits 0 - the tool re-runs this itself, it does not
trust the agent's claim. On failure: artifacts kept, `status:"fail"`,
compile/test log tails in the response (502-style semantics matching build
runs). Response envelope:

```json
{ "status": "pass|fail", "run_id": "...", "files": ["firmware/ser-des/serdes_rx_if_regs.h", ...],
  "compile_ok": true, "tests_ok": true, "test_output": "...tail...",
  "cost_usd": 1.87, "duration_s": 94 }
```

Run bookkeeping under the existing runs accounting (Token_Optimizer
visibility). Expected cost per run: low single-digit dollars (one agent
invocation + local gcc; no simulation).

---

## (e) v1 scope

IN:

1. `backend/firmware_blocks.py` + `GET /api/firmware_blocks` (catalog (a),
   per-PHY map, NO models key, namespace assertion test).
2. `frontend/src/firmwareArchitectures.js` + `firmwareArchStore`
   ("fw-arch-" namespace) + `ArchitectureDiagram variant="firmware"`;
   `ddr`/`hbm` alias `lpddr`.
3. Overview Firmware block (SW styling + caption) with click-to-focus.
4. Fifth stacked panel + `POST /api/fw_chat` (Firmware_Coder persona,
   interface as read-only context, firmware-catalog patch validation,
   `fw-chat-session-<phy>`, apply/undo, `fw_chats/` transcripts).
5. `POST /api/fw_generate`: regs.h + driver + stub + host tests + Makefile
   into `<working_dir>/firmware/<phy_type>/`, server-side
   `gcc -std=c99 -Wall -Werror` + test-run honesty check.

OUT (deferred, do not build speculatively):

- Any RTOS (schedulers, tasks, semaphores) - v1 firmware is a bare-metal
  polled model.
- Real target toolchains (arm-none-eabi, riscv-gcc), linker scripts,
  startup code - host gcc only; Firmware_Coder flags when a target
  toolchain would matter.
- IRQ simulation / ISR-context testing - the IRQ handler is a catalog +
  diagram block only; the generated dispatcher stub is polled.
- Co-simulation of firmware against RTL/behavioral models (DPI, VPI,
  iverilog+C harness) - the stub register model is the only execution
  target in v1.
- Codegen beyond the register layer: no supervisor/algorithm code
  (training, cal policy), no host API implementation, no field packing,
  no interrupt wiring. Each is its own future spec.
- Firmware diagram Save/Load UI (same deferral as the digital view).

Sequencing for Analog_Tool_Dev/Graphics_Dev: 1 is independent and can land
first with catalog-shape tests; 5 is backend-only and testable with a
stubbed agent (the deterministic map rule makes golden-file tests easy);
2+3+4 are frontend and mirror already-landed patterns (digital panel,
if_chat) almost mechanically. Real-money smoke for 5: one authorized run on
the shipped SerDes default interface - it must produce a passing `make test`
with exactly 15 registers per the map rule applied to the default table:
7 control (ctle_peak...adapt_hold) + 3 status (pll_locked, cdr_locked,
sig_detect) + 1 reset (deser_rstn; rstn_por is ext -> excluded) + 4 power
(en_*); clock and data rows excluded.

---

## Analog_Tool_Dev implementation status - v1 LANDED (2026-08-23)

Everything in scope items 1-5 except the firmware diagram's visual accent
(Graphics_Dev handoff below). All conventions per the digital-arch spec.

### Backend
- `backend/firmware_blocks.py`: catalog (a) verbatim + `FIRMWARE_BLOCKS_BY_PHY`.
  NO `models` key on any option (asserted by test); payload `note` carries the
  honesty line. Namespace disjoint from both the analog and digital catalogs
  (asserted). Served by `GET /api/firmware_blocks` -> `{groups, by_phy, note}`.
- `backend/arch_chat.py`: `validate_patch`/view machinery gained
  `view="firmware"` (firmware catalog, `kind:"iface"`, off-per-PHY soft
  warnings via `FIRMWARE_BLOCKS_BY_PHY`); `run_arch_consultant` gained
  `agent=`/`knowledge_dir=` kwargs (defaults unchanged - AFE/digital/interface
  chats bit-identical).
- `backend/fw_chat.py` + `POST /api/fw_chat` / `GET /api/fw_chat/{session}`:
  if_chat's exact envelope/session/422/502/transcript semantics, `fw_chats/`
  root in settings. Invocation = `claude --agent Firmware_Coder` (read-only
  tool allowlist, `~/.claude/agent-knowledge/digital-microarch/` on the read
  path). Prompt states the Firmware_Coder charter framing, the diagram
  semantics from (b) (drv layering rule, `csr-if` = stored interface stand-in,
  `host-if` = v1-stub), the four review duties, and the interface as READ-ONLY
  source of truth. Patch validates against the firmware catalog only.
- `backend/fw_generate.py` + `POST /api/fw_generate` (synchronous, minutes):
  deterministic register map (`compute_register_map` - exact spec rule,
  incl. group order and RW/RO), `if_c_name` ("-"->"_"), fixed deliverable set,
  prompt embedding the exact expected register table so the generator can't
  drift from the rule. Output to `<working_dir>/firmware/<phy_type>/`
  (cwd-scoped `Write(**)`; `disallowed_tools()` denies reused from
  circuit_builder). SERVER-ENFORCED honesty check: the backend re-runs a
  direct `gcc -std=c99 -Wall -Werror` compile of the deliverables (catches a
  Makefile that drops the flags) AND `make -C <dir> test`; `status:"pass"`
  only if both exit 0 - the agent's claim is never trusted. On failure:
  artifacts kept, log tail in `test_output`. Bookkeeping under
  `<working_dir>/fw_runs/<run_id>/` (spec.json/status.json/prompt/session
  logs/verify.log, cost_usd + duration_s recorded - Token_Optimizer
  visibility). `settings.py`: `fw_chats`/`firmware`/`fw_runs` subtrees.
- Tests: `backend/test_firmware.py` (22, all stubbed-agent, $0): catalog
  shape/no-models/namespace/by_phy, firmware-view patch validation (accept/
  reject/iface/soft-warn), fw_chat endpoint (transcript round-trip in
  fw_chats/, catalog validation, 502, 422), register-map rule (minimal +
  the shipped SerDes default = exactly the 15 registers in spec order),
  fw_generate with REAL gcc/make against stub-written fixtures: a good
  fixture must PASS, a deliberately-broken one (unused var under -Werror)
  must FAIL with `compile_ok:false`, a failing-test fixture FAILs with
  `compile_ok:true/tests_ok:false`, missing deliverables FAIL, agent failure
  reported, 422s (bad phy_type incl. traversal, invalid interface, no
  CSR-reachable signals). Full backend suite: 98 passed.

### Frontend
- `frontend/src/firmwareArchitectures.js`: `FIRMWARE_ARCHITECTURES` verbatim
  from (b), `ddr`/`hbm` alias `lpddr`, `firmwareArchStore =
  makeArchStore("fw-arch-", ...)`, `FIRMWARE_DESCRIPTIONS` hint lines.
- `frontend/src/FirmwarePanel.jsx`: fifth stacked panel body - diagram
  (`store=firmwareArchStore`, `variant="firmware"`, catalog optgroups with
  the `by_phy` typical set sorted first, selection visual-only, no models UI
  anywhere) + the "Generate register layer" action: inline cost-bearing
  confirm ("one paid Firmware_Coder run, a few $ / several minutes" + the
  honesty-check contract), pending spinner, results view with PASS/FAIL +
  compile + unit-test badges, generated-file list, collapsible compile/test
  log tail (auto-open on failure), cost/duration meta.
- `frontend/src/App.jsx`: `firmware` workspace ("PHY / Firmware", bottom of
  the chain), `fwVersion` bump wiring through `handleArchEdited("firmware")`.
  ALSO (user request, same day): panels now start COLLAPSED by default -
  fresh load shows only the overview expanded; all four workspace panels
  open via overview blocks (`handleOverviewFocus` un-minimize+scroll,
  unchanged) or their headers. Minimize state is not persisted, so this
  default applies every load.
- `frontend/src/ArchChatSidebar.jsx`: `firmware` workspace meta
  (`fw-chat-session-<phy>`, Firmware chat title/hints), sendTurn -> `fwChat`
  with `firmwareArchStore.currentArchitecture` + `loadIfDef(phyType)` as
  read-only interface context, apply/undo through `firmwareArchStore`
  (one undo slot, unchanged pattern).
- `frontend/src/PhyOverviewPanel.jsx`: Firmware block between Controller and
  [SoC core/host] - `.phy-overview-firmware` (distinct warm fill + dashed
  border) + "SW" tag + control-plane caption under the chain ("Data path is
  Controller <-> SoC direct"); click focuses/expands/scrolls the firmware
  panel via the existing `handleOverviewFocus`.
- `frontend/src/api.js`: `getFirmwareBlocks`, `fwChat`, `getFwChatTranscript`,
  `fwGenerate`. `frontend/src/ArchitectureDiagram.jsx`: `variant="firmware"`
  accepted - behaves exactly like "digital" (select-only, iface anchors, no
  build captions/legend), `data-variant` attribute added as the CSS hook,
  PNG export prefixed `firmware-`. CSS in `App.css` (`.fw-*`,
  `.phy-overview-firmware`, `.phy-overview-caption`).

### Verification (all driven in a real browser, dev servers up)
- `backend/tests/browser_check_firmware.py` (NEW, 46/46, network
  intercepted - $0): panel placement/expand, ser-des diagram content (11
  blocks incl. both iface anchors, optgroups, no model checkboxes), overview
  Firmware block (dashed computed style, SW tag, position between Controller
  and SoC, caption, click-to-focus + chat switch +
  `fw-chat-session-ser-des`), intercepted fw_chat turn (reply + patch card,
  Apply adds block via `fw-arch-` records, Undo restores), intercepted
  fw_generate PASS and FAIL renderings. Screenshots:
  `2026-08-23-firmware-panel.png`, `2026-08-23-firmware-generate-{pass,fail}.png`.
- `backend/tests/browser_check_overview.py` extended (39/39): NEW
  collapsed-by-default fresh-load check (exactly one expanded panel - the
  overview) + Firmware added to the overview-navigation cases; all
  pre-existing checks still green.
- REAL-MONEY SMOKE (the one authorized run): `POST /api/fw_generate` on the
  shipped SerDes default interface -> **status "pass"**, run `259c33579e43`,
  $2.79, 323 s. The tool's own re-run of `gcc -std=c99 -Wall -Werror` + the
  16-check unit-test suite exited 0; `serdes_rx_if_regs.h` contains exactly
  the 15 spec-mandated registers at offsets 0x0000-0x0038 in the exact rule
  order (RO = pll_locked/cdr_locked/sig_detect). Artifacts in
  `firmware/ser-des/`; verify log in `fw_runs/259c33579e43/verify.log`.

### Graphics_Dev handoff - firmware diagram accent (the one open bit)
- `ArchitectureDiagram.jsx` already accepts `variant="firmware"` and treats
  it as a select-only control view; it currently REUSES the digital styling
  (teal blocks, indigo iface anchors). Wanted per spec (b): a distinct
  amber/orange accent (your call on exact colors) so firmware and controller
  views are instantly tellable apart. Hooks in place: `data-variant="firmware"`
  on `.arch-diagram`, and the style branch in the block render (add a
  `FIRMWARE_BLOCK_STYLE` next to `DIGITAL_BLOCK_STYLE` and select on
  `variant` instead of the boolean; nothing else needs touching - the panel,
  store, picker and chat wiring are all live). `.firmware-panel` +
  `.fw-arch`-side CSS can mirror `.digital-arch-panel .arch-diagram-scroll`'s
  border/canvas tint (App.css). Keep: no model captions/legend, iface
  treatment identical to digital, PNG prefix `firmware-` (done).
- Overview block styling is DONE (dashed + SW tag + caption) - no action
  unless you want to align its palette with the diagram accent you pick.
- Please verify per the standing rule (drive it in a browser);
  `backend/tests/browser_check_firmware.py` is the free harness to extend.
- STATUS (Graphics_Dev, 2026-08-23): DONE - amber accent (FIRMWARE_BLOCK_STYLE #fffbeb/#d97706, amber selection #fde68a/#b45309, dashed borders + SW corner tags, warm canvas tint via `.arch-diagram[data-variant="firmware"]`), iface anchors unchanged indigo; verified headless on all four PHY types (layouts legible, no overlap, no firmwareArchitectures.js changes needed), 35/35 visual checks + 46/46 harness checks, screenshots `2026-08-23-fw-accent-*.png`.

### Not done (per spec section e - deferred, do not build speculatively)
RTOS/ISR simulation, target toolchains, co-simulation, codegen beyond the
register layer, firmware diagram Save/Load UI, dedicated ddr/hbm diagrams.
