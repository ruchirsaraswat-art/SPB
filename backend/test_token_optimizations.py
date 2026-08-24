"""
Stubbed tests for the Token_Optimizer audit changes of 2026-08-23
(IMPROVEMENTS-2026-08-21.md "## Token_Optimizer audit (Aug 23 evening)"):

  1. model routing   - one table (invocation.MODEL_ROUTING); the right
                       --model argument reaches the claude CLI per run type
                       (verified against a stub CLI capturing argv);
  2. fail-fast       - run-dir write preflight aborts with $0 spent before
                       the CLI is spawned; prompts open with the write
                       preflight line and close restating the summary schema;
  3. cost booking    - failed/cancelled/timed-out runs record the last cost
                       event from claude_stream.jsonl (synthetic streams +
                       the real failed-run fixture under runs/, read-only);
  4. cost cap        - rtl_generate controller scope aborts incrementally at
                       the (default $10, request-overridable) cap with an
                       honest "aborted: cost cap" reason.

NO real claude invocation happens anywhere in this file - the CLI is a
shell-script stub that records its argv and prints canned stream events.

Run:  cd backend && .venv/bin/python -m pytest test_token_optimizations.py -q
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

import pytest

import arch_chat
import circuit_builder as cb
import fw_generate as fw
import invocation as inv
import rtl_generate as rtl

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fixtures / helpers


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    wd = tmp_path / "workdir"
    settings_file.write_text(json.dumps({"working_dir": str(wd)}))
    monkeypatch.setenv("ANALOG_SPEC_TOOL_SETTINGS", str(settings_file))
    return wd


RESULT_EVENT = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "total_cost_usd": 0.05, "duration_ms": 10, "result": "stub reply",
    "session_id": "stub-session",
})


def make_stub_cli(tmp_path: Path, args_file: Path, lines: list[str], tail: str = "") -> str:
    """A fake `claude` binary: records its argv (one per line) into
    args_file, prints the given stream-json lines, then optionally runs
    `tail` (e.g. `exec sleep 30` to simulate a wedged session)."""
    script = tmp_path / "stub_claude.sh"
    body = "#!/bin/sh\n" + f'printf \'%s\\n\' "$@" > {shlex.quote(str(args_file))}\n'
    for line in lines:
        body += f"printf '%s\\n' {shlex.quote(line)}\n"
    if tail:
        body += tail + "\n"
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


def mirror_spec(**overrides):
    spec = {
        "topology": "nmos_current_mirror", "iref_ua": 10, "ratio_out": 2,
        "ratio_ref": 1, "vdd_v": 1.8, "w_ref_um": 1.0, "l_ref_um": 0.5,
        "corner": "tt",
    }
    spec.update(overrides)
    return spec


def recorded_args(args_file: Path) -> list[str]:
    return args_file.read_text().splitlines()


def model_flag_value(args: list[str]) -> str | None:
    return args[args.index("--model") + 1] if "--model" in args else None


# ---------------------------------------------------------------------------
# 1. Model routing - the table itself


def test_routing_cheap_run_types_go_to_sonnet():
    for run_type in ("fw_generate", "arch_chat", "if_chat", "fw_chat",
                     "veriloga", "verilog", "rnm", "arch_model"):
        assert inv.model_for(run_type) == "sonnet", run_type
        assert inv.model_args(run_type) == ["--model", "sonnet"], run_type


def test_routing_frontier_run_types_inherit():
    for run_type in ("full", "schematic_only", "symbol", "arch_stitch",
                     "rtl_block", "rtl_controller", "research"):
        assert inv.model_for(run_type) is None, run_type
        assert inv.model_args(run_type) == [], run_type


def test_routing_unknown_run_type_never_silently_downgrades():
    assert inv.model_for("some_future_run_type") is None
    assert inv.model_args("some_future_run_type") == []


def test_routing_table_covers_every_circuit_builder_deliverable():
    for deliverable in cb.DELIVERABLES:
        assert deliverable in inv.MODEL_ROUTING


# ---------------------------------------------------------------------------
# 1. Model routing - the argument actually reaching the CLI, per spawn site


def test_circuit_builder_rnm_run_passes_sonnet(tmp_path, workdir, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(cb, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    run_dir = tmp_path / "run"
    cb.run_circuit_builder(
        mirror_spec(deliverable="rnm", topology="ctle", extra_fields={}),
        run_dir, timeout_s=30,
    )
    args = recorded_args(args_file)
    assert model_flag_value(args) == "sonnet"
    assert args[args.index("--agent") + 1] == "Circuit_Builder"


def test_circuit_builder_full_run_inherits_fable(tmp_path, workdir, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(cb, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    cb.run_circuit_builder(mirror_spec(), tmp_path / "run", timeout_s=30)
    assert "--model" not in recorded_args(args_file)


def test_fw_generate_passes_sonnet_explicitly(tmp_path, workdir, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(fw, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    fw_dir = tmp_path / "fwdir"
    run_dir = tmp_path / "run"
    fw_dir.mkdir()
    run_dir.mkdir()
    out = fw.run_firmware_coder("stub prompt", fw_dir, run_dir, timeout_s=30)
    assert out["ok"] is True
    args = recorded_args(args_file)
    # Explicit --model sonnet even though the Firmware_Coder charter also
    # says `model: sonnet` - the flag wins over frontmatter (CLI docs) and
    # keeps the routing visible in code.
    assert model_flag_value(args) == "sonnet"
    assert args[args.index("--agent") + 1] == "Firmware_Coder"


def test_rtl_coder_controller_inherits_fable(tmp_path, workdir, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(rtl, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = rtl.run_rtl_coder("stub prompt", run_dir, timeout_s=30, run_type="rtl_controller")
    assert out["ok"] is True
    args = recorded_args(args_file)
    assert "--model" not in args
    assert args[args.index("--agent") + 1] == "RTL_Coder"


def test_consultant_chats_pass_sonnet(tmp_path, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(arch_chat, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    result = arch_chat.run_arch_consultant("q", tmp_path / "logs", "turn", timeout_s=30)
    assert result.status == "success"
    assert model_flag_value(recorded_args(args_file)) == "sonnet"


# ---------------------------------------------------------------------------
# 2. Fail-fast: preflight write check ($0 spent) + prompt framing


def _make_readonly(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)


def test_preflight_write_check_helper(tmp_path):
    assert inv.preflight_write_check(tmp_path) is None
    ro = tmp_path / "ro"
    _make_readonly(ro)
    try:
        msg = inv.preflight_write_check(ro)
        assert msg is not None and "preflight write check failed" in msg
        assert "$0 spent" in msg
    finally:
        ro.chmod(0o755)


def test_circuit_builder_aborts_before_spawn_on_unwritable_run_dir(
    tmp_path, workdir, monkeypatch
):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(cb, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    run_dir = tmp_path / "ro_run"
    _make_readonly(run_dir)
    try:
        result = cb.run_circuit_builder(mirror_spec(), run_dir, timeout_s=30)
    finally:
        run_dir.chmod(0o755)
    assert result.status == "failed"
    assert "preflight write check failed" in result.reason
    assert result.cost_usd == 0.0  # $0 spent, booked as exactly that
    assert not args_file.exists()  # the CLI was never spawned


def test_rtl_coder_aborts_before_spawn_on_unwritable_run_dir(tmp_path, workdir, monkeypatch):
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(rtl, "CLAUDE_BIN", make_stub_cli(tmp_path, args_file, [RESULT_EVENT]))
    run_dir = tmp_path / "ro_run"
    _make_readonly(run_dir)
    try:
        out = rtl.run_rtl_coder("p", run_dir, timeout_s=30)
    finally:
        run_dir.chmod(0o755)
    assert out["ok"] is False and "preflight write check failed" in out["reason"]
    assert out["cost_usd"] == 0.0
    assert not args_file.exists()


def test_build_prompts_open_with_preflight_and_close_with_summary_reminder():
    for spec in (
        mirror_spec(),  # full, current mirror
        mirror_spec(topology="ctle", extra_fields={}),  # full, generic
        mirror_spec(deliverable="schematic_only"),
        mirror_spec(deliverable="rnm", topology="ctle", extra_fields={}),
    ):
        prompt = cb.build_prompt(spec)
        assert prompt.startswith(cb.PROMPT_PREFLIGHT_LINE), spec.get("deliverable")
        assert "STOP IMMEDIATELY" in cb.PROMPT_PREFLIGHT_LINE
        # Schema requirement restated within the final 10 lines.
        tail = "\n".join(prompt.rstrip().splitlines()[-10:])
        assert "summary.json" in tail and "FINAL REMINDER" in tail, spec.get("deliverable")


def test_fw_and_rtl_prompts_carry_fail_fast_framing(workdir):
    fw_prompt = fw.build_fw_generate_prompt(
        "ser-des", {"name": "t", "signals": [], "power_sequence": [], "reset_sequence": []},
        "t", [{"signal": "s", "offset": 0, "width": 1, "access": "RW",
               "group": "control", "meaning": ""}],
    )
    assert fw_prompt.startswith(cb.PROMPT_PREFLIGHT_LINE)

    arch = {"blocks": [{"id": "rx-fifo", "topology": "elastic_fifo", "label": "FIFO"}],
            "edges": []}
    rtl_prompt = rtl.build_rtl_prompt(
        "block", "ser-des", arch, arch["blocks"],
        {"rx-fifo": {"depth_words": 16, "ppm_tolerance": 300}},
        {"name": "t", "signals": []}, [], [], True,
    )
    assert rtl_prompt.startswith(cb.PROMPT_PREFLIGHT_LINE)
    tail = "\n".join(rtl_prompt.rstrip().splitlines()[-10:])
    assert "rtl_summary.json" in tail and "FINAL REMINDER" in tail


# ---------------------------------------------------------------------------
# 3. Cost booking from the stream


def test_last_stream_cost_from_result_event(tmp_path):
    stream = tmp_path / "s.jsonl"
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init"}) + "\n"
        + json.dumps({"type": "assistant", "message": {}}) + "\n"
        + json.dumps({"type": "result", "total_cost_usd": 1.234}) + "\n"
    )
    assert inv.last_stream_cost_usd(stream) == 1.234


def test_last_stream_cost_from_model_usage_sum(tmp_path):
    stream = tmp_path / "s.jsonl"
    stream.write_text(json.dumps({
        "type": "result",
        "modelUsage": {"claude-fable-5": {"costUSD": 2.5}, "claude-haiku-4-5": {"costUSD": 0.01}},
    }) + "\n")
    assert inv.last_stream_cost_usd(stream) == pytest.approx(2.51)


def test_last_stream_cost_none_when_no_cost_event(tmp_path):
    stream = tmp_path / "s.jsonl"
    stream.write_text(json.dumps({"type": "assistant", "message": {"usage": {}}}) + "\n")
    assert inv.last_stream_cost_usd(stream) is None
    assert inv.last_stream_cost_usd(tmp_path / "missing.jsonl") is None


def test_last_stream_cost_real_failed_run_fixture():
    """The real failed run 5617f4d784f5 (status failed, stream carries a
    result event with total_cost_usd 1.2285743) - read-only."""
    fixture = REPO_ROOT / "runs" / "5617f4d784f5" / "claude_stream.jsonl"
    if not fixture.is_file():
        pytest.skip("real failed-run fixture not present")
    assert inv.last_stream_cost_usd(fixture) == pytest.approx(1.2285743)


def test_timed_out_run_books_stream_cost(tmp_path, workdir, monkeypatch):
    """A wedged session (result event flushed, then hangs) must book the real
    cost on the timeout path instead of None."""
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(
        cb, "CLAUDE_BIN",
        make_stub_cli(tmp_path, args_file, [RESULT_EVENT], tail="exec sleep 30"),
    )
    result = cb.run_circuit_builder(mirror_spec(), tmp_path / "run", timeout_s=2)
    assert result.status == "failed" and "timed out" in result.reason
    assert result.cost_usd == pytest.approx(0.05)  # from the stream, not None


# ---------------------------------------------------------------------------
# 4. Incremental cost cap (rtl_generate controller scope)


def _assistant_usage_event(model: str, output_tokens: int) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"model": model,
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0,
                              "output_tokens": output_tokens}},
    })


def test_stream_cost_estimator_math():
    est = inv.StreamCostEstimator()
    # 100k output tokens on fable-5: 5x multiplier * $12/MTok = $6.00
    est.feed_line(_assistant_usage_event("claude-fable-5", 100_000))
    assert est.cost_usd == pytest.approx(6.0)
    # plus 100k output tokens on sonnet: 5 * $3.5/MTok = $1.75
    est.feed_line(_assistant_usage_event("claude-sonnet-5", 100_000))
    assert est.cost_usd == pytest.approx(7.75)
    assert not est.is_exact


def test_stream_cost_estimator_exact_result_wins():
    est = inv.StreamCostEstimator()
    est.feed_line(_assistant_usage_event("claude-fable-5", 100_000))
    est.feed_line(json.dumps({"type": "result", "total_cost_usd": 0.42}))
    assert est.is_exact and est.cost_usd == 0.42


def test_rtl_cost_cap_aborts_incrementally(tmp_path, workdir, monkeypatch):
    """Session streams enough usage to cross the cap, then wedges: the loop
    must terminate it with an honest 'aborted: cost cap' reason, booking the
    estimated spend - never waiting out the 90 min timeout."""
    args_file = tmp_path / "args.txt"
    monkeypatch.setattr(
        rtl, "CLAUDE_BIN",
        make_stub_cli(
            tmp_path, args_file,
            [_assistant_usage_event("claude-fable-5", 100_000)],  # ~$6 estimated
            tail="exec sleep 30",
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = rtl.run_rtl_coder("p", run_dir, timeout_s=60,
                            run_type="rtl_controller", cost_cap_usd=5.0)
    assert out["ok"] is False
    assert out["reason"].startswith("aborted: cost cap")
    assert out["cost_usd"] == pytest.approx(6.0, rel=0.01)
    # the honest abort is also in the human log
    assert "aborted: cost cap" in (run_dir / "session.log").read_text()


def test_rtl_cap_defaults_controller_10_block_none(tmp_path, monkeypatch):
    captured = {}

    def fake_coder(prompt, run_dir, timeout_s, cancel_event=None,
                   run_type=None, cost_cap_usd=None):
        captured[run_type] = cost_cap_usd
        return {"ok": False, "reason": "stub", "cost_usd": 0.0, "raw_text": ""}

    monkeypatch.setattr(rtl, "run_rtl_coder", fake_coder)
    monkeypatch.setattr(rtl, "normalize_interface", lambda i: {})
    monkeypatch.setattr(rtl, "validate_interface", lambda i: ([], []))
    monkeypatch.setattr(rtl, "validate_rtl_request", lambda *a, **k: ([], []))
    monkeypatch.setattr(rtl, "get_answer_status", lambda p: {"answer": "no_spec"})
    monkeypatch.setattr(rtl, "resolve_docs_for_generation", lambda *a, **k: [])
    monkeypatch.setattr(rtl, "build_rtl_prompt", lambda *a, **k: "p")

    base = {"phy_type": "ser-des", "digital_architecture": {"blocks": []},
            "interface": {}, "library": "lib"}
    rtl.execute_rtl_run({**base, "scope": "controller"}, tmp_path)
    rtl.execute_rtl_run({**base, "scope": "block"}, tmp_path)
    rtl.execute_rtl_run({**base, "scope": "controller", "cost_cap_usd": 25.0}, tmp_path)
    assert captured["rtl_controller"] == 25.0  # last controller call (override)
    assert captured["rtl_block"] is None  # no default cap for block scope


def test_rtl_cap_default_is_10(tmp_path, monkeypatch):
    captured = {}

    def fake_coder(prompt, run_dir, timeout_s, cancel_event=None,
                   run_type=None, cost_cap_usd=None):
        captured["cap"] = cost_cap_usd
        return {"ok": False, "reason": "stub", "cost_usd": 0.0, "raw_text": ""}

    monkeypatch.setattr(rtl, "run_rtl_coder", fake_coder)
    monkeypatch.setattr(rtl, "normalize_interface", lambda i: {})
    monkeypatch.setattr(rtl, "validate_interface", lambda i: ([], []))
    monkeypatch.setattr(rtl, "validate_rtl_request", lambda *a, **k: ([], []))
    monkeypatch.setattr(rtl, "get_answer_status", lambda p: {"answer": "no_spec"})
    monkeypatch.setattr(rtl, "resolve_docs_for_generation", lambda *a, **k: [])
    monkeypatch.setattr(rtl, "build_rtl_prompt", lambda *a, **k: "p")

    rtl.execute_rtl_run(
        {"phy_type": "ser-des", "digital_architecture": {"blocks": []},
         "interface": {}, "library": "lib", "scope": "controller"},
        tmp_path,
    )
    assert captured["cap"] == rtl.RTL_CONTROLLER_COST_CAP_USD == 10.0
