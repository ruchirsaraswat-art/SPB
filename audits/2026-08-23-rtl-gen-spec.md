# 2026-08-23 - Spec-document intake + RTL generation (`rtl` deliverable): feature spec

Author: Digital_Microarchitect. Implementable spec for two user requests in the
PHY/Controller workspace: (a) specification-document intake when controller
architecting starts, and (b) RTL generation of controller blocks / the whole
controller, executed by the RTL_Coder agent (`~/.claude/agents/RTL_Coder.md`)
with a server-enforced honesty check.

Conventions this spec deliberately mirrors (do not invent parallel ones):

- Deliverable-run machinery = `audits/2026-08-21-deliverable-modes.md`
  (run records with `deliverable` + timeouts, `evaluate_summary()`-style
  post-run check, library filing with provenance, ResultsView sections,
  cost-bearing confirm).
- Honesty pattern = `backend/models_contract.py` (`PASS_TOKENS`, "another
  program cross-checks the files and pass tokens on disk and will downgrade
  a false verified to failed"). Here we go one step further and RE-RUN the
  verification server-side (fw_generate precedent: backend re-runs gcc + tests).
- Digital catalog / diagram / interface data = `audits/2026-08-23-digital-arch-spec.md`
  (catalog fields are the RTL parameters; interface signal table is the port
  contract; diagram edges are the stitch netlist).
- UI naming: "Controller" is the display name for the digital side; internal
  ids/namespaces stay `digital`/`dig-`.

Toolchain fact this relies on: `iverilog -g2012` + `vvp` are installed and
already used for Verilog/RNM model verification, so RTL runs never need a
`generated_unverified` status - every run is verifiable on this machine.

---

## (a) Specification-document intake

### Trigger

The first time the user focuses the PHY/Controller workspace OR sends the
first controller-view chat message for a given PHY type, and no spec-doc
answer is recorded for that PHY, the controller panel shows a compact banner
(not a modal - never block the workspace):

> "Is there a specification document for this controller (JEDEC, PCIe, UCIe,
> CXL, Ethernet, proprietary)? RTL generation and the controller chat will
> use it as the requirements source."
> Buttons: **Attach file** | **Paste reference** | **No spec - first principles**
> plus a dismiss "x" (dismiss = ask again next session; an explicit choice =
> never auto-ask again for this PHY).

A permanent small **"Spec docs (N)"** button in the controller panel header
reopens the same UI at any time (list existing docs, add/remove, edit
relevant sections, change the "no spec" answer).

### Storage

Files: `<working_dir>/spec_docs/<phy_type>/<doc_id>.<ext>` (uploaded copy;
`doc_id` = slugified title, 1-64 `[a-z0-9_-]`, uniquified with `-2` suffix).
Metadata: one JSON per doc next to it, `<doc_id>.meta.json`:

```json
{
  "id": "jesd209-4b",
  "title": "JESD209-4B LPDDR4",
  "standard_family": "JEDEC",           // free text; UI suggests: JEDEC, PCIe, UCIe, CXL, Ethernet, USB, MIPI, proprietary, other
  "version": "B, Feb 2017",             // free text
  "source": { "type": "file", "path": "spec_docs/lpddr/jesd209-4b.pdf" },
      // OR {"type": "url", "url": "https://..."}
      // OR {"type": "reference", "citation": "JESD209-4B section 7 (no file on hand)"}
  "user_notes": "We only implement the x16 single-channel subset.",
  "relevant_sections": [
    { "label": "Mode registers", "locator": "section 7.2, pages 45-58",
      "applies_to": ["controller", "interface"] },
    { "label": "Training", "locator": "section 4.4, pages 120-133",
      "applies_to": ["controller", "firmware"] }
  ],
  "applies_to_workspaces": ["controller", "interface", "firmware"],
  "phy_type": "lpddr",
  "added_at": "2026-08-23T14:02:00Z"
}
```

Plus one per-PHY answer file `<working_dir>/spec_docs/<phy_type>/_status.json`:
`{"answer": "attached" | "no_spec" | null, "answered_at": ...}` - this is what
suppresses the banner.

API (mirrors interfaces/architectures storage conventions - roots search,
newest first):

| endpoint | behavior |
|---|---|
| `GET /api/spec_docs?phy_type=` | list metadata (+ `_status`) across working dirs |
| `POST /api/spec_docs` | multipart upload (file + metadata JSON) or metadata-only (url/reference source). 422 on bad id/enum/missing title. File size cap 40 MB. Accepted types: pdf, txt, md, html (others stored but marked `"unreadable_by_agent": true`). |
| `PUT /api/spec_docs/{phy}/{id}` | metadata edit (relevant_sections is the common edit) |
| `DELETE /api/spec_docs/{phy}/{id}` | remove file + metadata |
| `POST /api/spec_docs/{phy}/answer` | set `_status.answer` ("no_spec" or "attached") |

### How spec docs feed context (the pragmatic rule)

Never inline document contents into a prompt. Two consumption modes:

1. **Chat context (arch_chat view=digital, if_chat, fw_chat)**: the request
   gains optional `spec_docs: [ ...metadata JSON objects... ]` (frontend sends
   the ones whose `applies_to_workspaces` includes the current workspace).
   The server renders them into the prompt as a compact block - title, family,
   version, user_notes, and the relevant_sections list (label + locator) -
   capped at 2000 chars total (truncate `user_notes` first, then drop docs
   beyond the first 3 with a "N more docs attached" line). The consultant is
   told these exist and that it should cite section locators when advising,
   and to say so when an answer really requires reading the document (it
   cannot read files in chat - only generation runs can).
2. **Generation context (rtl_generate below, fw_generate)**: the run prompt
   receives the metadata block PLUS the absolute file path, and the
   instruction: "Read ONLY the relevant_sections locators that apply to the
   blocks you are generating (PDF `pages` ranges, max 20 pages per Read), plus
   at most 10 additional pages you judge necessary (table of contents lookup
   allowed). Never read the whole document. Cite section numbers in RTL
   comments for every non-obvious requirement taken from the document
   (RTL_Coder charter rule). If a needed section is not listed and you cannot
   locate it within the extra-page budget, report the gap as a finding instead
   of guessing." URL sources: the agent may WebFetch the URL with the same
   selectivity instruction; `reference`-type sources are context-only (cite,
   don't read). If `answer == "no_spec"`, the prompt states requirements come
   solely from the catalog fields + interface definition, first-principles.

Relevant-section designation is the USER's job (the metadata editor renders
`relevant_sections` as an editable list with a hint: "give page ranges - the
generator reads only what you list, plus a small budget"). v1 does no
automatic parsing/indexing of documents (OUT, section d).

---

## (b) RTL generation contract (`rtl` deliverable)

### Endpoint and request

`POST /api/rtl_generate` (own endpoint like `fw_generate`, because digital
blocks are a separate namespace and must never enter the Circuit_Builder
analog flow; run records still carry `deliverable: "rtl"` and appear in
`GET /api/runs` with cost/duration/status like every other run).

```json
{
  "scope": "block" | "controller",
  "phy_type": "ser-des",
  "digital_architecture": { "blocks": [...], "edges": [...] },   // current effective diagram
  "block_id": "rx-align",                       // scope=block only: a designable diagram block
  "block_fields": { "rx-align": { "parallel_width_bits": 16, "align_pattern": "K28.5", ... },
                    "rx-fifo": { ... } },       // catalog field values per block id (see UI, c)
  "interface": { ...full interface definition as stored... },
  "spec_doc_ids": ["jesd209-4b"],               // optional; resolved server-side to metadata+paths
  "library": "serdes_ctrl",                     // required: filing destination
  "cell": null                                  // optional override; defaults below
}
```

Validation (422, all problems listed at once, deliverable-modes style):
unknown/iface/non-designable `block_id`; a block whose topology is not in the
digital catalog; scope=controller with zero designable blocks; missing
`block_fields` entry or missing required numeric field for any included block
(fields with `min` defined and no value = required; text fields may be empty
and default per catalog help text - the prompt says which defaults were
assumed); interface hard-errors present (run `validate_interface` first -
warnings pass through into the prompt, hard errors block); unknown
`spec_doc_ids`; missing library.

### Deliverables and filenames

`<block>` = diagram block id, `-` mapped to `_` (block ids are unique per
diagram; two instances of the same catalog topology - e.g. the 8b/10b encoder
and decoder - therefore get distinct modules/files, which is intended).
Module name == file stem.

| scope | files (all in the run dir, then filed) |
|---|---|
| block | `<block>.v`, `<block>_tb.sv`, `<block>_sim.log`, `rtl_summary.json` |
| controller | one `<block>.v` + `<block>_tb.sv` + `<block>_sim.log` per designable block in the diagram, `<phy>_controller_top.v` (phy_type `-`→`_`), `<phy>_controller_top_tb.sv`, `<phy>_controller_top_sim.log`, `rtl_summary.json` |

`rtl_summary.json` (the machine-checked claim, models-contract style):

```json
{
  "scope": "controller",
  "blocks": [
    { "block": "rx_align", "topology": "word_aligner",
      "files": { "rtl": "rx_align.v", "tb": "rx_align_tb.sv", "sim_log": "rx_align_sim.log" },
      "status": "verified" | "failed",
      "status_reason": "",
      "checks": [ {"name": "lock_all_offsets", "result": "pass"}, ... ],
      "spec_citations": ["JESD209-4B 7.2"] }
  ],
  "top": { "module": "ser_des_controller_top", "files": {...}, "status": "...", "checks": [...] },
  "findings": [ "edge csr->train has no named signal contract in the interface; wired per CSR map section X" ]
}
```

### Synthesizable-subset rules (verbatim mirror of the RTL_Coder charter - the
prompt restates them; the charter is the source of truth)

- Verilog-2005 style RTL: `always @(posedge clk)`, synchronous resets unless
  the interface definition marks the reset async (e.g. `rstn_por` async
  assert / sync release - then the recognized async-assert pattern,
  commented), no `#` delays or initial-driven logic in DUT code, no latches
  unless explicitly intended and commented, no `$display` in DUT.
- SystemVerilog only in testbenches (`-g2012` covers both).
- CDC crossings use recognized synchronizer patterns (2ff for levels, async
  FIFO with gray-coded pointers for buses, pulse stretch/toggle-sync for
  events) and each is commented `// CDC:` with the from/to domains - these
  must agree with the interface definition's `cdc_points` table; a crossing
  in the RTL with no cdc_points entry is a finding.
- Widths/depths/timeouts parameterized from catalog fields
  (`parameter DEPTH_WORDS = 16` etc.), never hard-coded. **All timeout and
  settle counters must be parameters** so testbenches can shrink them
  (mandated - the FSM TB criteria below depend on it).
- Requirements precedence: spec document (cited) > interface definition >
  catalog fields > agent judgment (documented in a header comment).
  Conflicts and unwireable edges are reported as `findings`, never silently
  improvised around (charter rule).

### Whole-controller stitching rules

- Top module `<phy>_controller_top`. **AFE-boundary ports come verbatim from
  the interface definition's signal table**: `a2d` → `input`, `d2a` →
  `output`, `ext` clocks/resets → `input`, exact names, `[width-1:0]` for
  width > 1. Every interface signal must appear as a port; a signal no block
  consumes is still a port, tied off with a `// finding:` comment and listed
  in `findings`.
- Core-side and host-side iface anchor nodes map to top-level port groups
  named per the relevant block fields (CSR `bus_protocol` APB →
  `psel/penable/pwrite/paddr[A-1:0]/pwdata[D-1:0]/prdata[D-1:0]/pready`;
  protocol engine `flow_control` valid/ready → `core_rx_data/valid/ready`,
  `core_tx_data/valid/ready`; DFI blocks → `dfi_*` names from the interface
  default).
- Internal wiring follows the digital diagram edges. An edge between two
  blocks whose data contract is not derivable (no shared signal in the
  interface, no obvious datapath width match) is a reported finding plus a
  best-effort wire ONLY if widths match exactly; otherwise left unconnected
  and reported (failing the run is not required - the finding + a top TB that
  still passes its listed checks is acceptable for v1).
- Clock domains per the interface `clock_domains` table; each module gets the
  clock its diagram position implies (datapath blocks up to the elastic FIFO
  on the AFE word clock, FIFO read side onward on the core clock, CSR/FSMs on
  the host/ref clock).

### Self-checking testbench requirements

Universal (every TB, block and top):

- Ends by printing exactly one token: `RTL_TB_PASS` or `RTL_TB_FAIL <reason>`
  then `$finish`. Any `$fatal`/assertion path must print the fail token first.
- Watchdog: an independent `initial` block that after `WATCHDOG_CYCLES`
  (>= 10x expected test length, computed from the test's own loop counts)
  prints `RTL_TB_FAIL watchdog_timeout` and finishes - a hung DUT must never
  stall vvp forever (same rule as the models contract).
- Self-checking against a reference model/queue in the TB - zero reliance on
  waveform inspection. Each named check below appears in the `checks` list.
- Randomized stimulus uses a fixed seed (reproducibility of the server re-run).

Per block class (concrete criteria - the prompt includes the class list for
the blocks in the run; numbers reference the block's own catalog field values):

**FIFO class** (`elastic_fifo`, `dfi_read_path` capture FIFO, TX FIFO):

| check | criterion |
|---|---|
| fill_to_full | from empty, write `depth_words` words with reads held off: `full` asserts on exactly the `depth_words`-th write, not before |
| overflow_guard | 8 further write attempts while full: no pointer movement, subsequent readout of all `depth_words` words matches the reference queue exactly |
| drain_to_empty | read all words: `empty` asserts exactly after the last; 8 read attempts while empty do not advance the read pointer |
| pointer_wrap | streaming push+pop for >= 4 x `depth_words` words at matched rate; every output word compared to the reference queue, 0 mismatches |
| ppm_stress | writer clock offset from reader by the `ppm_tolerance` field value (implement as period difference in the TB) for >= 10,000 words: no overflow/underflow; if `skip_symbol` is set, verify skip insert/delete events occur and data (skips excluded) still matches |
| flags | almost_full/almost_empty (if implemented) assert at their parameterized thresholds, checked at threshold-1 / threshold / threshold+1 occupancy |

**Aligner class** (`word_aligner`, `lane_deskew`):

| check | criterion |
|---|---|
| lock_all_offsets | for EVERY bit offset 0..`parallel_width_bits`-1: stream words containing `align_pattern` at that offset; lock asserts within `lock_threshold_patterns` + `align_latency_cycles` + 8 word clocks |
| no_false_lock | 1,000 pattern-free random words (TB masks out accidental pattern matches or regenerates): lock never asserts |
| passthrough | after lock, 1,000 random words pass through bit-exact (offset-compensated reference) |
| realign | mid-stream, shift the input phase by a random nonzero offset: lock deasserts within the design's loss threshold and re-asserts at the new offset within the lock_all_offsets bound |
| deskew (lane_deskew only) | lane-to-lane skews of 0, `deskew_range_ui`/2 and `deskew_range_ui` UI: outputs word-aligned across all `num_lanes`; skew of `deskew_range_ui`+1 reported as out-of-range status, not silently wrong data |

**CSR class** (`csr_regfile`):

| check | criterion |
|---|---|
| reset_values | after reset, every register reads its documented reset value |
| rw_fields | every RW field: write 0xA5A5A5A5-pattern then 0x5A5A5A5A-pattern (masked to field width), read back exact both times; neighboring fields in the same register unchanged |
| ro_fields | every RO field: bus write is ignored (readback unchanged); value tracks the hardware input port within 1 bus-read of a port change |
| w1c_fields | every W1C field: hw event sets bit; read 1; write 1 clears; write 0 leaves set; a hw event in the same cycle as the clearing write wins (bit stays set) - or the documented alternative, checked either way |
| decode | read of >= 4 unmapped addresses returns 0 (or the `bus_protocol` error response) with zero register side effects |
| protocol | >= 20 back-to-back bus transfers with 0 and with random 1-3 cycle wait states: all complete per protocol (APB: PREADY semantics) |

**FSM class** (`link_training_fsm`, `power_state_fsm`, `d2d_link_state_fsm`,
`zq_cal_fsm`, `ddr_training_engine`, `cal_engine`, `eye_monitor_ctrl`):

| check | criterion |
|---|---|
| state_coverage | TB visits every state (count == `num_states` / documented state list); TB tallies distinct states seen on the state output port and fails if any missing |
| nominal_sequence | with status inputs (pll_locked, cdr_locked, comparator, ...) asserted after plausible delays, FSM reaches ACTIVE/DONE; enable outputs assert in exactly the interface power_sequence step order, one step at a time |
| timeout_paths | for EVERY wait-on-status step: withhold that status (all others normal); FSM enters its ERROR/retry state within timeout + 2 cycles (timeouts overridden to small values via the mandated parameters, e.g. 32 cycles) |
| retry_policy | where the sequence documents "retry N then ERROR": exactly N retries observed, then ERROR, no more |
| algorithm (cal/eye/training engines) | close the loop against a TB-side behavioral plant (e.g. comparator = sign(trim - hidden target)): engine converges to the hidden target within +/-1 LSB in <= the documented step bound (binary search: `trim_resolution_bits` + 2 iterations; linear sweep: 2^bits steps); repeated for 5 randomized hidden targets incl. both endpoints |

**Codec class** (`line_codec_8b10b`, `scrambler_descrambler`,
`crc_retry_engine`, `gearbox`, `prbs_gen_checker`):

| check | criterion |
|---|---|
| roundtrip_8b10b | all 256 data bytes x both starting disparities (512 cases) + all 12 K-codes encode→decode bit-exact; running disparity stays in {-1,+1} after every symbol |
| error_detect_8b10b | >= 20 known-invalid 10b codes flag code error; >= 20 disparity-violating (legal-code, wrong-RD) symbols flag disparity error; `disparity_error_action` behavior verified; pipeline latency equals `latency_cycles` |
| roundtrip_scrambler | 10,000 random words scramble→descramble == input; self-sync mode: one injected bit error produces exactly (1 + number of feedback taps) output errors then clean recovery; additive mode: wrong seed detected / resync per design |
| crc_retry | 1,000 clean flits accepted; single-bit corruption at 64 random (flit, bit) positions: every one detected, NAK/retry issued, replay from the retry buffer delivers the original flit in order; sustained full-rate traffic with round-trip delay such that exactly `retry_buffer_flits` are outstanding: no stall, no overwrite |
| gearbox_ratio | stream >= 100 x LCM(`input_width_bits`, `output_width_bits`)/`input_width_bits` input words: output bitstream is a bit-exact reassembly, wrap boundaries included |
| prbs | generator→checker: lock, then 0 errors over 100,000 bits; 10 injected single-bit errors counted as exactly 10; counter saturates (does not wrap) at 2^`error_counter_bits`-1 |

**DFI path blocks** (`dfi_command_path`, `dfi_write_path`, `dfi_read_path`):
treated as FIFO + gearbox composites plus one timing check each - command
slots per `dfi_ratio` land in the correct phase; `tphy_wrlat_cycles` /
`trddata_en_cycles` measured in the TB equal the field value exactly.

**Controller-top TB** (scope=controller; this is an integration smoke, not a
re-run of the block TBs):

| check | criterion |
|---|---|
| bringup | drive the interface reset/power sequence stimulus (assert status inputs per the sequence rows, timeouts parameter-shrunk): controller reaches ACTIVE; enable outputs observed in sequence order |
| csr_access | via the host bus: read >= 3 ID/status registers, write+readback >= 3 control registers through the real bus ports |
| rx_datapath | inject >= 1,000 words of correctly encoded data (e.g. 8b/10b-coded PRBS with the align pattern) at the AFE RX ports, with the word clock offset by the interface's documented ppm: data emerges at the core ports bit-exact after decode, 0 errors |
| tx_datapath | drive >= 1,000 core-side words: AFE TX port stream decodes back to the input bit-exact |
| timeout_smoke | withhold ONE status input (e.g. cdr_locked): controller reaches its ERROR-visible CSR state |
| port_conformance | TB instantiates the top using EVERY interface-table port by name (a compile-time check that the port contract is verbatim); TB fails if any port is missing/miswidthed - iverilog makes this a compile failure, which the honesty check catches |

### Verification protocol and server-enforced honesty check

Agent-side (in the prompt): for each block, and then the top,

```
iverilog -g2012 -o <block>_tb.vvp <block>.v <block>_tb.sv        # (top: plus all block .v files)
vvp <block>_tb.vvp | tee <block>_sim.log                          # exit 0 AND RTL_TB_PASS, no RTL_TB_FAIL
```

Report per-block `status` honestly in `rtl_summary.json` - "another program
re-runs everything and will downgrade a false verified to failed" (same
sentence pattern as models_contract).

Server-side `evaluate_rtl_summary()` + re-run (this is the pass/fail of the
RUN, like model-only deliverables where a failed model fails the run):

1. Every file claimed in `rtl_summary.json` exists on disk; every deliverable
   filename from the table above is claimed. Missing → run failed.
2. For each block and the top, the server ITSELF re-runs the iverilog compile
   + vvp commands above (fresh output names, captured log). Pass iff exit 0
   AND literal `RTL_TB_PASS` in the captured log AND no `RTL_TB_FAIL`. The
   agent's claimed status is overwritten by the server result; a downgrade is
   recorded in `status_reason` ("claimed verified; server re-run failed:
   ...") - exactly the `check_models_in_summary` downgrade semantics, made
   stronger by re-execution (fixed TB seeds make this deterministic).
3. Run verdict: scope=block - the block must verify. scope=controller - the
   run is `success` iff ALL blocks AND the top verify; `partial` (new badge,
   amber) iff the top or >= 1 block failed but >= 1 block verified - verified
   blocks are still filed (they are individually useful); `failed` otherwise.
4. Per-check watchdog on the re-run itself: 300 s per vvp invocation, kill →
   that block failed with reason "server re-run watchdog".

Timeouts and cost (run metadata, `timeout_for` extension): scope=block
1800 s (comparable to schematic_only); scope=controller 5400 s. Neither is a
`light_run`. Expected cost, to be refined after the first real runs: per-block
$2-5 (more code than the $1.08 rnm smoke, similar shape), whole-controller
roughly N_blocks x per-block plus integration - **$15-40 for a typical 8-12
block diagram; the most expensive run type in the tool** (see UI, c).

### Library filing

Same machinery as other deliverables (`backend/libraries.py`):

- scope=block: files into cell `<block>` (or the `cell` override) in the
  chosen library; views added: `rtl` (`<block>.v`), `rtl_tb`, `rtl_log`;
  provenance `deliverable: "rtl"`, merges surviving earlier views (a cell can
  hold an rnm model AND rtl).
- scope=controller: top files into cell `<phy>_controller_top`; each verified
  block additionally filed into its own `<block>` cell (same rule as
  arch_stitch back-filing generated symbols). Failed blocks are not filed;
  their files remain in the run dir for inspection.

---

## (c) UI

### Triggers

- **Per-block**: the digital (Controller) diagram's block selection is
  currently visual-only. Selecting a designable block now also populates a
  small **"Block RTL"** menu under the diagram (ArchGenerateMenu visual
  pattern): the block's catalog fields as a mini-form (same
  label/unit/help/min/max/warn rendering as the spec form), a library picker
  (+ optional cell override), spec-doc chips (which attached docs will be
  passed, from `applies_to_workspaces`), and a **"Generate RTL - <block>"**
  button with a cost-bearing confirm ("~$2-5, up to 30 min"). Field values
  persist per PHY + block id in localStorage `dig-fields-<phy>` (JSON
  `{block_id: {field: value}}`) - this record is new and is exactly what
  `block_fields` in the request carries; it also finally gives the dormant
  `adapt_targets` interface warning something to check against (wire that
  hook when convenient, not required for v1). Required-field gaps disable the
  button with an inline list. Iface anchors and blocks show nothing.
- **Whole controller**: a **"Generate controller RTL"** button in the
  controller panel toolbar (next to the topology picker area). Opens a
  confirmation card listing: the N designable blocks included (iface anchors
  skipped, same note style as the arch menu's pads note), per-block field
  completeness (blocks with missing required fields listed - generation
  blocked until fixed), interface status (hard errors block; warnings shown),
  spec-doc status (attached docs, or "no spec declared" with a link to the
  intake banner), library/cell destination, and the cost line - verbatim
  tone: **"This is the most expensive run type in this tool: estimated
  $15-40 and 30-90 minutes for N blocks. It generates and verifies every
  block plus the stitched top."** Generate stays disabled until a library is
  chosen (arch_stitch precedent).

### Results presentation

ResultsView gains an "RTL" section (deliverable badge "RTL - block" /
"RTL - controller"; controller runs may carry the `partial` amber badge):

- Per block: a row with status badge (verified green / failed red, server
  verdict), file links (`<block>.v`, `<block>_tb.sv`, sim log), the checks
  list (name + pass/fail), and downgrade reasons prominently when the server
  overruled the agent.
- Controller runs add the top row first (module name, port count vs the
  interface signal count - mismatch is impossible if port_conformance passed,
  so show the count as confirmation), the `findings` list verbatim (these are
  the contract inconsistencies RTL_Coder is chartered to report), and the
  filed-into cell links per block.
- Spec citations (`spec_citations`) rendered as chips per block so the user
  sees which document sections drove the RTL.
- Filed like other deliverables: the library browser shows the new
  `rtl`/`rtl_tb`/`rtl_log` views on the cell.

---

## (d) v1 scope

IN:

1. `spec_docs` storage + API + intake banner + "Spec docs (N)" manager +
   chat-context block (arch_chat digital view, if_chat, fw_chat) + the
   selective-Read generation rule.
2. `POST /api/rtl_generate` (scope block|controller), validation, RTL_Coder
   prompt with the synthesizable-subset rules, per-class TB criteria tables,
   `rtl_summary.json` contract.
3. Server honesty check: `evaluate_rtl_summary()` + full iverilog/vvp re-run,
   downgrade semantics, `partial` verdict, per-invocation 300 s watchdog.
4. Library filing of rtl/rtl_tb/rtl_log views; run records with
   `deliverable: "rtl"`, timeouts 1800/5400 s.
5. UI: per-block "Block RTL" menu (fields form + `dig-fields-<phy>` store),
   "Generate controller RTL" button + confirm card with the explicit
   most-expensive-run cost warning, ResultsView RTL section.

OUT (explicitly deferred - do not build speculatively):

- Synthesis, timing/SDC, PPA numbers, OpenLane - no synthesis claims of any
  kind (RTL_Coder charter mirror; iverilog functional verification only).
- Formal verification, UVM, assertions-based (SVA) checking, and coverage
  METRICS collection (the state_coverage check is a TB-counted criterion, not
  a coverage-database flow).
- Spec-document parsing/indexing beyond the user-listed sections + selective
  Read (no automatic TOC extraction, no embedding search, no requirement
  tracing matrices).
- Register-map export formats (SystemRDL/CSV) - the CSR RTL is generated
  from its catalog fields + interface signals, but no map artifact ships.
- Lint (verilator --lint) gating - worth adding to the honesty check later if
  verilator is installed; not assumed present.
- Regenerating RTL incrementally on diagram/interface edits (every run is
  from-scratch; filed views are simply replaced).
- RNM/mixed-signal co-simulation of the generated RTL against AFE models
  (natural follow-up: the elastic FIFO + aligner against the deserializer
  RNM model - its own spec when asked).

Sequencing note for the implementers: item 1 (spec_docs) and items 2-4
(rtl_generate backend) are independent of item 5 and of the App.jsx work
currently in flight; the backend can land first with table-driven tests in
the `test_deliverables.py` style (validation 422s, prompt fragments per block
class, evaluate_rtl_summary downgrade cases with planted logs/tokens, re-run
against a tiny known-good and known-bad fixture module, filing incl. the
partial-verdict selective filing). First real run recommendation: one cheap
per-block smoke (`elastic_fifo`, small depth) before authorizing any
whole-controller run, to calibrate the cost estimate shown in the UI.

---

## Implementation status (v1, 2026-08-24 - Analog_Tool_Dev)

IMPLEMENTED. Everything in the v1 IN list is built and green; suites:
backend 117 passed (was 98 pre-feature; 19 new in `backend/test_rtl.py`),
browser 46/46 in `backend/tests/browser_check_rtl.py` plus the pre-existing
overview (39/39) and firmware (46/46) suites unregressed.

### Where things live

- `backend/spec_docs.py` - spec-doc storage (`spec_docs/<phy>/<doc_id>.<ext>`
  + `<doc_id>.meta.json` + `_status.json`), slug/uniquify, 40 MB cap,
  pdf/txt/md/html readable vs `unreadable_by_agent`, all-problems-at-once
  422s, `render_chat_context()` (2000-char cap: user_notes truncated first,
  then docs beyond 3 dropped with an "N more docs attached" line),
  `resolve_docs_for_generation()`.
- `backend/rtl_generate.py` - request validation, block-class map + the
  per-class TB criteria tables (quoted verbatim into the prompt), the
  RTL_Coder prompt (charter-restated synthesizable-subset rules, verbatim
  AFE-boundary port rule, selective-Read spec-doc instruction / no_spec
  first-principles statement), `run_rtl_coder` (`claude --agent RTL_Coder`,
  fw_generate-style stream loop + cancel), `evaluate_rtl_summary()` (file
  presence/claim check, then a REAL server-side iverilog -g2012 + vvp re-run
  per block and top with fresh output names, 300 s per-vvp watchdog,
  check_models_in_summary downgrade semantics recorded in `status_reason`,
  success/partial/failed verdict), and rtl/rtl_tb/rtl_log library filing
  (verified modules only; provenance merges surviving views).
- `backend/main.py` - `GET/POST /api/spec_docs`, `PUT/DELETE
  /api/spec_docs/{phy}/{id}`, `POST /api/spec_docs/{phy}/answer`;
  `POST /api/rtl_generate` runs in a background thread under
  `<working_dir>/runs/<run_id>/` with `deliverable: "rtl"` so RTL runs appear
  in `GET /api/runs` (cost/duration/status, cancellable) like every other
  run. Timeouts 1800 s (block) / 5400 s (controller). `spec_docs` context
  field added to arch_chat (digital view), if_chat and fw_chat.
- Frontend: `SpecDocsPanel.jsx` (intake banner + "Spec docs (N)" manager;
  dismiss x = sessionStorage, ask again next session; explicit answer =
  server `_status.json`), `RtlGenerateMenu.jsx` (Block RTL menu with the
  catalog-fields mini-form persisted in `dig-fields-<phy>`, library picker,
  spec-doc chips, $2-5/30 min confirm; Generate controller RTL launcher +
  cost-confirm card), wired through `DigitalArchPanel.jsx` /
  `App.jsx`; `ResultsView.jsx` RTL section (per-block verdict badges from
  the SERVER result, checks, downgrade reasons, spec-citation chips,
  findings verbatim, filed-cell links, amber `partial` badge).

### Deviations from the letter of this spec (deliberate)

1. File attach is a server-side path copy (JSON `source: {type: "file",
   path: "<absolute local path>"}`; the backend copies the file into
   `spec_docs/<phy>/`), not a multipart upload - per the implementation
   note ("pick the simpler that works locally"); backend and browser share
   this machine, and it avoids a new python-multipart dependency. The
   stored artifact is still the spec'd uploaded COPY under `spec_docs/`.
2. `POST /api/rtl_generate` is asynchronous ({run_id} + poll
   `GET /api/runs/{id}`) rather than fw_generate-style synchronous - a
   5400 s HTTP request is not a workable frontend contract, and this is
   what makes RTL runs first-class rows in the runs list per this spec's
   own requirement.
3. The dormant `adapt_targets` interface-warning hook against
   `dig-fields-<phy>` was not wired (spec: "when convenient, not required
   for v1").

### Calibration appendix - first real run (the spec's recommended smoke)

One real `rtl_generate` executed 2026-08-24 (run `20df115378b5`):
scope=block, ser-des `elastic_fifo` (diagram block `rx-fifo`), depth_words
16 / width_bits 16 / ppm_tolerance 300 / skip_symbol blank, default ser-des
interface, filed into `serdes_ctrl`.

- Verdict: **success** - and the honesty check passed FOR REAL: the server's
  own fresh `iverilog -g2012` compile + `vvp` re-run
  (`rx_fifo_sim.server.log`) printed `RTL_TB_PASS` with all six FIFO-class
  checks: fill_to_full, overflow_guard, drain_to_empty, pointer_wrap
  (128 words), ppm_stress (2 x 20,000 words at +/-300 ppm period offset,
  0 overflow/underflow, SKP delete=6/insert=4, 0 data mismatches skips
  excluded), flags (AF/AE at threshold-1/threshold/threshold+1). That is
  full coverage of the FIFO criteria table above, incl. the judging trio
  (full-at-depth, wrap, ppm stress).
- 5 findings reported (assumed K28.0 SKP for the blank skip_symbol field,
  the rx_err_* CDC rows lacking a diagram edge through the FIFO, the
  16-vs-20-bit raw-word question, reset-domain expectations on pwr-seq,
  rate-compensation sizing judgment) - exactly the
  report-don't-improvise behavior the charter mandates.
- Filed: `libraries/serdes_ctrl/rx_fifo/` with views `rx_fifo.v` (rtl),
  `rx_fifo_tb.sv` (rtl_tb), `rx_fifo_sim.log` (rtl_log), provenance
  `deliverable: "rtl"`.
- **Actual cost/duration: $4.21, 491 s (8.2 min).** Within the $2-5 /
  30 min per-block band (high side - the elastic FIFO is one of the harder
  blocks: async gray pointers + SKP logic).
- Pro-rata recalibration of the controller estimate: a typical 8-12 block
  diagram at $4.21 / 8.2 min per block puts a whole-controller run at
  ~$34-51 + top ≈ **$25-55 and 60-120 minutes** - the original $15-40 /
  30-90 min band was low. The UI's controller confirm card and the
  ResultsView running text now carry the recalibrated numbers ($2-5 /
  30 min per-block band unchanged). Refine again after the first real
  controller run (NOT executed - needs explicit user approval).
