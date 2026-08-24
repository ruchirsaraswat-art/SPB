# Token_Optimizer audit — tool-side changes applied (2026-08-23)

Applies the user-approved tool-side recommendations from the Token_Optimizer
audit summarized in `IMPROVEMENTS-2026-08-21.md` § "Token_Optimizer audit
(Aug 23 evening)". No real paid runs were made for this change; all
verification is stubbed/free.

## 1. Model routing (audit item 1)
- ONE routing table: `backend/invocation.py` `MODEL_ROUTING`, consumed via
  `model_args(run_type)` by every spawn site. Sonnet-routed: `fw_generate`,
  `arch_chat`/`if_chat`/`fw_chat`, model-only deliverables
  (`veriloga`/`verilog`/`rnm`) and `arch_model`. Inherit (fable-5): `full`,
  `schematic_only`, `symbol`, `arch_stitch`, `rtl_block`, `rtl_controller`,
  `research`. Unknown run types inherit (never a silent downgrade).
- Mechanism verified against the Claude Code docs (code.claude.com/docs
  sub-agents + cli-reference): the `--model` CLI flag WINS over an agent
  charter's `model:` frontmatter. Firmware_Coder's charter already says
  `model: sonnet`; the tool passes `--model sonnet` explicitly anyway so the
  routing is visible in code and immune to charter drift (both mechanisms
  agree today).
- Wired in: `circuit_builder.run_circuit_builder` (by deliverable),
  `fw_generate.run_firmware_coder`, `rtl_generate.run_rtl_coder` (new
  `run_type` param), `arch_chat.run_arch_consultant` (new `run_type` param;
  if_chat/fw_chat wrappers pass theirs), `circuit_researcher` (explicit
  inherit).

## 2. Fail-fast (audit item 2)
- `invocation.preflight_write_check(run_dir)`: probe write BEFORE spawning
  the CLI at every spawn site (circuit_builder, fw_generate — both fw_dir
  and run_dir, rtl_generate). On failure: abort with a plain reason,
  `cost_usd = 0.0`, CLI never spawned.
- `circuit_builder.PROMPT_PREFLIGHT_LINE` now opens every build prompt
  (all circuit_builder deliverables, fw_generate, rtl_generate): "first
  action: confirm you can write in the run dir; if a write is
  permission-denied, stop immediately and report — no workarounds".
- Summary schema restated within the final 10 lines of every long prompt
  that requires one: `PROMPT_SUMMARY_REMINDER` (summary.json, all
  circuit_builder deliverables) and an equivalent rtl_summary.json reminder
  at the end of the RTL prompt.

## 3. Cost booking + caps (audit item 4)
- Failed/cancelled/timed-out runs now book the last cost event parsed from
  `claude_stream.jsonl` (`invocation.last_stream_cost_usd`) instead of
  recording None. The stream read loops also DRAIN remaining CLI output
  after terminate, so a result event flushed on the way down is captured.
  Applied to circuit_builder, fw_generate, rtl_generate, arch/if/fw chat,
  circuit_researcher.
- Incremental cost cap on rtl_generate controller scope: default
  `RTL_CONTROLLER_COST_CAP_USD = 10.0`, overridable per request
  (`cost_cap_usd` on POST /api/rtl_generate, block scope has no default
  cap). No mid-stream event carries dollars (verified against every stream
  on disk), so the loop keeps a running usage-based estimate
  (`invocation.StreamCostEstimator`; per-model $/MTok rates fitted against
  this tool's own real result events — haiku 1.0, sonnet 3.5, fable 12.0,
  rounded up so the estimate errs high). On crossing: honest abort — status
  failed, reason "aborted: cost cap …", partial (estimated) spend booked.
- `circuit_builder.DEFAULT_TIMEOUT_S` trimmed 2400 → 1800 s (schematic_only
  now equals the full-run timeout; ordering assertion in
  tests/test_deliverables.py updated accordingly).

## 4. Split-run guidance (audit finding 3 — soft UI only, no pipeline change)
- SpecForm: a blue informational hint appears under the Generate dropdown
  when the full flow is selected AND model checkboxes are ticked ("cheaper:
  run the build first, then model-only runs against the filed cell"); the
  models-group help text carries the same note. Nothing blocks; the bundled
  run still works.

## 5. Scoped verification (audit finding 6 — the rule, recorded here)
Browser verification is now SCOPED: after a UI change, run only the browser
suite(s) covering the touched panel(s); the full three-suite sweep runs at
most once per session. This change touched only the spec form, so only a
spec-form check was driven (ad-hoc playwright script, 7/7 checks passed;
screenshot `2026-08-23-specform-split-run-cost-hint.png`). The firmware /
overview / rtl suites were deliberately NOT run.

## Status / verification summary
- Backend: **141 passed** (the pre-change 117 all green + 24 new stubbed
  tests in `backend/test_token_optimizations.py` covering routing table +
  per-spawn-site `--model` argv via a stub CLI, preflight aborts ($0
  booked, CLI not spawned), stream cost parsing (synthetic streams + the
  real failed run `runs/5617f4d784f5` read-only), estimator math, and the
  incremental cap abort + default/override wiring).
- Browser: scoped spec-form check only, 7/7 passed (see item 5).
- Cost of this change: $0 in agent runs (stubs only; docs consulted for the
  --model precedence question instead of a live probe run).
