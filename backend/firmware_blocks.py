"""
Firmware block catalog for the firmware workspace (feature spec:
audits/2026-08-23-firmware-section-spec.md, section a).

Served by GET /api/firmware_blocks in the same {groups, by_phy} shape as
/api/digital_blocks, with ONE deliberate difference: NO "models" key on any
option. Firmware is host-compiled C (gcc), not Verilog - the
veriloga/verilog/rnm behavioral-model levels do not apply and must not be
offered. The frontend keys the model-checkbox UI off the presence of
"models"; firmware options omit it entirely, so the models UI stays off with
zero special-casing. The firmware equivalent of "model verification" is the
fw_generate codegen contract (backend/fw_generate.py): gcc compile + host
unit test, re-run and enforced by the tool itself.

Separate namespace: nothing here is added to topologies.TOPOLOGY_GROUPS or
the digital catalog (asserted by test, same as digital). Firmware diagram
`topology` strings validate against this catalog only (arch_chat
view="firmware" via POST /api/fw_chat).

Field schema style mirrors topologies.TOPOLOGY_FIELDS / digital_blocks
exactly (name/label/unit/help, optional min/max, type:"text").
"""

from __future__ import annotations

# Honesty note carried in the GET /api/firmware_blocks payload.
FIRMWARE_MODELS_NOTE = (
    "Firmware blocks have no behavioral-model levels: firmware is host-compiled "
    "C (gcc), verified by compile + unit test, not by iverilog."
)

FIRMWARE_BLOCK_GROUPS: list[dict] = [
    {
        "group": "Boot & power",
        "options": [
            {
                "value": "boot_init_sequencer",
                "label": "Boot / init sequencer",
                "fields": [
                    {"name": "boot_time_budget_ms", "label": "Boot-time budget", "unit": "ms", "min": 0,
                     "help": "Wall-clock from reset release to link ACTIVE. Floor = sum of the interface "
                     "power_sequence waits (e.g. SerDes default: ~1.5 ms of settle/lock timeouts); budget "
                     "must exceed it."},
                    {"name": "num_init_steps", "label": "Init step count (estimate)", "unit": "", "min": 1,
                     "help": "Entries in the init table; should track the interface reset_sequence + "
                     "power_sequence step counts."},
                    {"name": "poll_interval_us", "label": "Status poll interval", "unit": "us", "min": 1,
                     "help": "Polling cadence for wait_for status bits (pll_locked, cdr_locked) when no IRQ "
                     "is wired."},
                    {"name": "on_timeout", "label": "Timeout policy", "unit": "", "type": "text",
                     "help": "Per-step give-up behavior: 'retry N then fail', 'fail-stop', 'degrade (skip "
                     "optional step)'. Mirrors the retry annotations in the interface power_sequence."},
                    {"name": "config_source", "label": "Config source", "unit": "", "type": "text",
                     "help": "'compiled-in defaults' or 'host-supplied config table' - decides whether trim "
                     "seeds live in the binary or come from the SoC."},
                ],
            },
            {
                "value": "power_state_manager",
                "label": "Power-state manager",
                "fields": [
                    {"name": "num_power_states", "label": "Power state count", "unit": "", "min": 2,
                     "help": "Mirrors the controller power_state_fsm states (e.g. OFF/SLEEP/STANDBY/ACTIVE); "
                     "firmware sequences entry/exit via CSRs."},
                    {"name": "wake_latency_budget_us", "label": "Wake latency budget", "unit": "us", "min": 0,
                     "help": "Deepest-sleep to ACTIVE; must cover PLL relock + CDR relock per the interface "
                     "sequence timeouts."},
                    {"name": "entry_policy", "label": "Sleep entry policy", "unit": "", "type": "text",
                     "help": "'host-commanded only' or 'inactivity timer'."},
                    {"name": "inactivity_timeout_ms", "label": "Inactivity timeout", "unit": "ms", "min": 0,
                     "help": "0 = no autonomous sleep entry."},
                ],
            },
        ],
    },
    {
        "group": "Register access & events",
        "options": [
            {
                "value": "reg_access_driver",
                "label": "Register-access driver layer (HAL)",
                "fields": [
                    {"name": "bus_protocol", "label": "Host bus protocol", "unit": "", "type": "text",
                     "help": "Must match the controller csr_regfile bus_protocol (APB3 for v1). Documentation "
                     "field - the driver only sees MMIO."},
                    {"name": "data_width_bits", "label": "Access width", "unit": "bits", "min": 8, "max": 64,
                     "help": "CSR access width; 32 recommended (one signal per 32-bit register in v1 codegen)."},
                    {"name": "base_addr", "label": "MMIO base address", "unit": "", "type": "text",
                     "help": "Hex base of the PHY CSR window in the SoC map, e.g. 0x4001_0000. Codegen emits "
                     "it as a single #define."},
                    {"name": "poll_timeout_us", "label": "Default poll timeout", "unit": "us", "min": 0,
                     "help": "Give-up default for read-poll helpers (wait-for-bit); per-call override allowed."},
                ],
            },
            {
                "value": "irq_event_handler",
                "label": "IRQ / event handler",
                "fields": [
                    {"name": "num_irq_sources", "label": "IRQ sources", "unit": "", "min": 1,
                     "help": "Status events routed as interrupts (lock-loss, training-done, cal-done, CRC "
                     "error...). Each must be an a2d status signal in the interface table."},
                    {"name": "irq_latency_budget_us", "label": "IRQ latency budget", "unit": "us", "min": 0,
                     "help": "Worst-case assert-to-handler-CSR-access; drives whether polling is acceptable "
                     "instead."},
                    {"name": "dispatch_model", "label": "Dispatch model", "unit": "", "type": "text",
                     "help": "'polled loop', 'vectored ISR', or 'mailbox to host' - v1 codegen only stubs a "
                     "polled dispatcher."},
                    {"name": "event_queue_depth", "label": "Event queue depth", "unit": "events", "min": 1,
                     "help": "Deferred-work queue between ISR context and supervisors."},
                ],
            },
        ],
    },
    {
        "group": "Calibration & training",
        "options": [
            {
                "value": "cal_supervisor",
                "label": "Calibration supervisor",
                "fields": [
                    {"name": "cal_interval_ms", "label": "Recalibration interval", "unit": "ms", "min": 0,
                     "help": "0 = boot-only. Periodic recal tracks V/T drift; must match/drive the controller "
                     "cal_engine and zq_cal_fsm interval fields."},
                    {"name": "cal_time_budget_us", "label": "Per-cal time budget", "unit": "us", "min": 0,
                     "help": "Ceiling per cal pass so periodic recal never stalls traffic beyond this."},
                    {"name": "num_cal_targets", "label": "Cal targets managed", "unit": "", "min": 1,
                     "help": "Trim buses this supervisor owns; each must be a d2a quasi_static signal in the "
                     "interface table (ctle_peak, vga_gain, slicer_ofst...)."},
                    {"name": "drift_trigger", "label": "Off-schedule trigger", "unit": "", "type": "text",
                     "help": "What forces recal outside the interval: 'temp-code delta', 'error-count "
                     "threshold', 'none'."},
                    {"name": "retry_limit", "label": "Cal retry limit", "unit": "", "min": 0,
                     "help": "Failed-cal retries before raising an event to the host."},
                ],
            },
            {
                "value": "training_supervisor",
                "label": "Link / DDR training supervisor",
                "fields": [
                    {"name": "training_timeout_ms", "label": "Training timeout", "unit": "ms", "min": 0,
                     "help": "Firmware-level give-up; must exceed the controller training FSM's own "
                     "training_timeout_ms."},
                    {"name": "max_retrain_attempts", "label": "Max retrain attempts", "unit": "", "min": 0,
                     "help": "Autonomous retrains before escalating to the host."},
                    {"name": "retrain_trigger", "label": "Retrain trigger", "unit": "", "type": "text",
                     "help": "'lock loss (cdr_locked/sig_detect)', 'CRC error rate', 'host command', "
                     "'frequency switch'."},
                    {"name": "adapt_targets", "label": "Adaptation targets", "unit": "", "type": "text",
                     "help": "Mirrors the controller link_training_fsm.adapt_targets / "
                     "ddr_training_engine.trainings; firmware owns policy + sequencing, hardware owns the "
                     "inner loop."},
                ],
            },
            {
                "value": "eye_sweep_service",
                "label": "Eye-monitor sweep service",
                "fields": [
                    {"name": "phase_steps", "label": "Phase steps across UI", "unit": "", "min": 2,
                     "help": "Must equal the controller eye_monitor_ctrl.phase_steps (and the eye_phase_code "
                     "width in the interface table)."},
                    {"name": "vref_steps", "label": "Vref steps", "unit": "", "min": 2,
                     "help": "Must equal eye_monitor_ctrl.vref_steps / eye_vref_code range."},
                    {"name": "samples_per_point", "label": "Samples per point", "unit": "", "min": 1,
                     "help": "Bounds the measurable BER floor per point (~1/samples)."},
                    {"name": "sweep_time_budget_ms", "label": "Full-sweep budget", "unit": "ms", "min": 0,
                     "help": "Sanity: >= phase_steps * vref_steps * samples_per_point / word_rate."},
                ],
            },
        ],
    },
    {
        "group": "Diagnostics & host",
        "options": [
            {
                "value": "diag_bist_orchestrator",
                "label": "Diagnostics / BIST orchestrator",
                "fields": [
                    {"name": "prbs_order", "label": "PRBS order", "unit": "", "min": 7, "max": 31,
                     "help": "Must match the controller prbs_gen_checker.prbs_order."},
                    {"name": "dwell_time_ms", "label": "Per-test dwell", "unit": "ms", "min": 0,
                     "help": "PRBS run length per measurement; with the word rate this sets the confidence "
                     "floor of the BER estimate."},
                    {"name": "error_threshold", "label": "Fail threshold", "unit": "errors", "min": 0,
                     "help": "Errors within the dwell that mark the test failed."},
                    {"name": "report_format", "label": "Report format", "unit": "", "type": "text",
                     "help": "'pass/fail bitmap', 'per-lane error counts', 'JSON blob via host API' - "
                     "documentation in v1."},
                ],
            },
            {
                "value": "host_api_service",
                "label": "Host / SoC API layer",
                "fields": [
                    {"name": "api_style", "label": "API style", "unit": "", "type": "text",
                     "help": "'blocking C calls' (v1 codegen target), 'async + callbacks', 'mailbox/doorbell'."},
                    {"name": "num_api_entry_points", "label": "API entry points (estimate)", "unit": "", "min": 1,
                     "help": "init/up/down/sleep/wake/status/diag... - the surface the SoC application sees."},
                    {"name": "error_model", "label": "Error model", "unit": "", "type": "text",
                     "help": "'negative return codes' (v1), 'errno-style', 'status struct'."},
                ],
            },
        ],
    },
    {
        "group": "Memory-PHY services (LPDDR/DDR/HBM)",
        "options": [
            {
                "value": "zq_recal_service",
                "label": "ZQ periodic recalibration service",
                "fields": [
                    {"name": "recal_interval_ms", "label": "Recal interval", "unit": "ms", "min": 0,
                     "help": "Drives the controller zq_cal_fsm.cal_interval_ms; 0 = boot-only."},
                    {"name": "zq_timeout_us", "label": "ZQ cal timeout", "unit": "us", "min": 0},
                    {"name": "on_fail", "label": "Failure policy", "unit": "", "type": "text",
                     "help": "'keep last code + event', 'retry', 'fail link'."},
                ],
            },
            {
                "value": "freq_switch_manager",
                "label": "Frequency-switch manager",
                "fields": [
                    {"name": "num_operating_points", "label": "Operating points", "unit": "", "min": 1,
                     "help": "Frequency set points (e.g. boot 50 MHz, mission 100/200 MHz dfi_clk)."},
                    {"name": "switch_time_budget_us", "label": "Switch time budget", "unit": "us", "min": 0,
                     "help": "Traffic-stopped window: PLL re-lock + retrain per the interface sequences."},
                    {"name": "retrain_on_switch", "label": "Retrain on switch", "unit": "", "type": "text",
                     "help": "'full', 'shortened (saved-code restore + verify)', 'none'."},
                ],
            },
        ],
    },
    {
        "group": "Die-to-die services (UCIe-style)",
        "options": [
            {
                "value": "sideband_msg_service",
                "label": "Sideband message service",
                "fields": [
                    {"name": "msg_timeout_ms", "label": "Message timeout", "unit": "ms", "min": 0,
                     "help": "Per-request timeout on the sideband mailbox (parameter exchange, remote CSR "
                     "access)."},
                    {"name": "num_msg_types", "label": "Message types (estimate)", "unit": "", "min": 1},
                    {"name": "param_exchange_policy", "label": "Param exchange policy", "unit": "", "type": "text",
                     "help": "'fixed table' or 'negotiated (advertise + resolve)' during SBINIT/PARAM."},
                ],
            },
            {
                "value": "lane_repair_service",
                "label": "Lane repair service",
                "fields": [
                    {"name": "repair_trigger", "label": "Repair trigger", "unit": "", "type": "text",
                     "help": "'boot BIST map only' or 'runtime per-lane error escalation'."},
                    {"name": "max_repairs", "label": "Max repairs", "unit": "lanes", "min": 0,
                     "help": "Bounded by the controller lane_repair_mux.num_spare_lanes."},
                ],
            },
        ],
    },
]

FIRMWARE_BLOCK_LABELS: dict[str, str] = {
    opt["value"]: opt["label"] for group in FIRMWARE_BLOCK_GROUPS for opt in group["options"]
}

ALL_FIRMWARE_BLOCK_VALUES: set[str] = set(FIRMWARE_BLOCK_LABELS)

# Which blocks the firmware view offers per PHY type. fw_chat soft-warns on
# off-map add_block, never rejects - same rule as digital.
_SERDES_FW = [
    "boot_init_sequencer", "power_state_manager", "reg_access_driver",
    "irq_event_handler", "cal_supervisor", "training_supervisor",
    "eye_sweep_service", "diag_bist_orchestrator", "host_api_service",
]
_MEM_FW = [
    "boot_init_sequencer", "power_state_manager", "reg_access_driver",
    "irq_event_handler", "training_supervisor", "zq_recal_service",
    "freq_switch_manager", "diag_bist_orchestrator", "host_api_service",
]

FIRMWARE_BLOCKS_BY_PHY: dict[str, list[str]] = {
    "ser-des": list(_SERDES_FW),
    "lpddr": list(_MEM_FW),
    "ddr": list(_MEM_FW),
    "hbm": list(_MEM_FW) + ["lane_repair_service"],
    "die-to-die": [
        "boot_init_sequencer", "power_state_manager", "reg_access_driver",
        "irq_event_handler", "training_supervisor", "sideband_msg_service",
        "lane_repair_service", "diag_bist_orchestrator", "host_api_service",
    ],
    "optical": list(_SERDES_FW),
}
