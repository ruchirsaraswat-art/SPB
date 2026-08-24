"""
Cross-cutting helpers for the tool's paid headless `claude` runs, added for
the Token_Optimizer audit of 2026-08-23 (IMPROVEMENTS-2026-08-21.md, "##
Token_Optimizer audit (Aug 23 evening)", items 1/2/4):

1. MODEL ROUTING (audit item 1) - the ONE routing table mapping each headless
   run type to the model it should pay for. Cheap/formulaic run types go to
   Sonnet; frontier work (full builds, schematic drawing, stitching, RTL)
   stays on the session default (fable-5 via inherit = no --model flag).

   Mechanism, verified against the Claude Code docs (code.claude.com/docs
   sub-agents + cli-reference, checked 2026-08-23): the `--model` CLI flag
   takes precedence over an agent charter's `model:` frontmatter field, which
   in turn beats settings/env. Firmware_Coder's charter was recently set to
   `model: sonnet`, which would already cover `--agent Firmware_Coder`
   invocations on its own - but frontmatter routing is invisible from this
   codebase and can drift, so every invocation site passes model_args()
   explicitly and this table is the single source of truth. Passing
   `--model sonnet` alongside `--agent Firmware_Coder` is therefore redundant
   but harmless (both mechanisms agree); if the charter ever changes, this
   table still wins.

2. RUN-DIR WRITE PREFLIGHT (audit item 2) - one observed run burned $0.99
   before dying on a write-permission error the backend could have caught for
   free. preflight_write_check() is called by every spawn site BEFORE the CLI
   subprocess starts; on failure the run aborts with a clear reason and
   cost_usd == 0.0.

3. COST BOOKING FROM THE STREAM (audit item 4) - failed/cancelled/timed-out
   runs used to record cost_usd = None even when the stream on disk contained
   a final result event with the real total_cost_usd (~$2.4 of spend went
   untracked). last_stream_cost_usd() parses the last cost-bearing event out
   of a claude_stream.jsonl. For runs terminated before any result event
   exists (timeout/cancel/cost-cap), StreamCostEstimator provides a running
   ESTIMATE from the per-turn assistant usage events - good enough for honest
   bookkeeping and for the incremental cost cap, and clearly labeled an
   estimate in the code paths that use it.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 1. Model routing

MODEL_SONNET = "sonnet"
MODEL_INHERIT = None  # no --model flag -> the CLI/session default (fable-5)

# Run type -> model. THE routing table (audit item 1) - every headless
# invocation site resolves its model here via model_args(); do not hard-code
# --model anywhere else.
MODEL_ROUTING: dict[str, str | None] = {
    # Cheap/formulaic run types -> Sonnet (measured ~$2.79 -> ~$0.7 for
    # fw_generate, ~$1.1 -> ~$0.3 for arch_model in the audit).
    "fw_generate": MODEL_SONNET,
    "arch_chat": MODEL_SONNET,
    "if_chat": MODEL_SONNET,
    "fw_chat": MODEL_SONNET,
    "veriloga": MODEL_SONNET,   # model-only deliverable runs
    "verilog": MODEL_SONNET,
    "rnm": MODEL_SONNET,
    "arch_model": MODEL_SONNET,
    # Frontier run types -> inherit (fable-5): transistor-level design,
    # schematic/symbol drawing, stitching, RTL, topology research.
    "full": MODEL_INHERIT,
    "schematic_only": MODEL_INHERIT,
    "symbol": MODEL_INHERIT,
    "arch_stitch": MODEL_INHERIT,
    "rtl_block": MODEL_INHERIT,
    "rtl_controller": MODEL_INHERIT,
    "research": MODEL_INHERIT,
}


def model_for(run_type: str) -> str | None:
    """The routed model for a run type; unknown types inherit (never silently
    downgrade a run type nobody classified as cheap)."""
    return MODEL_ROUTING.get(run_type, MODEL_INHERIT)


def model_args(run_type: str) -> list[str]:
    """The argv fragment to splice into the claude CLI command: ["--model",
    "<model>"] for routed types, [] for inherit."""
    model = model_for(run_type)
    return ["--model", model] if model else []


# --------------------------------------------------------------------------
# 2. Run-dir write preflight


def preflight_write_check(run_dir: Path) -> str | None:
    """Confirm the directory the agent will work in is actually writable
    BEFORE paying for a CLI session. Returns None when writable, else a
    plain-English reason to abort with ($0 spent). Writes and removes a
    uniquely named probe file (never a name an agent deliverable could use).
    """
    probe = Path(run_dir) / f".write_preflight_{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("preflight")
        probe.unlink()
    except OSError as exc:
        return (
            f"preflight write check failed: cannot write in {run_dir} "
            f"({exc.strerror or exc}) - aborting before spawning the CLI ($0 spent)"
        )
    return None


# --------------------------------------------------------------------------
# 3. Cost booking / estimation from claude_stream.jsonl


def last_stream_cost_usd(stream_path: Path) -> float | None:
    """Parse the LAST cost-bearing event out of a claude_stream.jsonl (the
    final `result` event's total_cost_usd, or a summed modelUsage costUSD if
    total_cost_usd is absent). Returns None if the stream has no cost event
    (e.g. the CLI was killed before emitting one) or the file is missing."""
    stream_path = Path(stream_path)
    if not stream_path.is_file():
        return None
    cost: float | None = None
    try:
        with open(stream_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or ("cost_usd" not in line and "costUSD" not in line):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev.get("total_cost_usd"), (int, float)):
                    cost = float(ev["total_cost_usd"])
                elif isinstance(ev.get("modelUsage"), dict):
                    summed = sum(
                        u.get("costUSD", 0.0)
                        for u in ev["modelUsage"].values()
                        if isinstance(u, dict)
                    )
                    if summed > 0:
                        cost = summed
    except OSError:
        return None
    return cost


# Per-model $/MTok INPUT-equivalent rates for the running estimate, with the
# standard multipliers (cache write 1.25x input, cache read 0.1x input,
# output 5x input). Fit against the real result events of this tool's own
# runs on disk (2026-08-23): haiku implied exactly 1.00, sonnet 3.24-3.39,
# fable-5 11.02-11.76. Rates are rounded UP slightly so the estimate errs
# high - this is for the cost cap and for booking runs that died before a
# real cost event, never a replacement for a real total_cost_usd.
_RATE_USD_PER_MTOK_INPUT = {
    "claude-haiku": 1.0,
    "claude-sonnet": 3.5,
    "claude-opus": 15.0,
    "claude-fable": 12.0,
}
_DEFAULT_RATE = 12.0  # unknown model: assume frontier pricing (err high)
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.1
_OUTPUT_MULT = 5.0


def _rate_for_model(model: str) -> float:
    for prefix, rate in _RATE_USD_PER_MTOK_INPUT.items():
        if model.startswith(prefix):
            return rate
    return _DEFAULT_RATE


class StreamCostEstimator:
    """Running cost ESTIMATE built up from stream-json events as they arrive
    (assistant events carry per-turn `message.usage`; no mid-stream event
    carries a dollar figure - verified against every stream on disk). Used by
    the rtl_generate incremental cost cap and, as a fallback, to book the
    spend of runs killed before the CLI emitted a result event.

    If a `result` event with total_cost_usd IS seen, that exact figure
    replaces the estimate."""

    def __init__(self) -> None:
        self._estimated = 0.0
        self._exact: float | None = None

    def feed_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        self.feed_event(ev)

    def feed_event(self, ev: dict[str, Any]) -> None:
        if ev.get("type") == "result" and isinstance(
            ev.get("total_cost_usd"), (int, float)
        ):
            self._exact = float(ev["total_cost_usd"])
            return
        if ev.get("type") != "assistant":
            return
        message = ev.get("message") or {}
        usage = message.get("usage") or {}
        if not usage:
            return
        weighted = (
            usage.get("input_tokens", 0)
            + _CACHE_WRITE_MULT * usage.get("cache_creation_input_tokens", 0)
            + _CACHE_READ_MULT * usage.get("cache_read_input_tokens", 0)
            + _OUTPUT_MULT * usage.get("output_tokens", 0)
        )
        self._estimated += weighted * _rate_for_model(message.get("model") or "") / 1e6

    @property
    def cost_usd(self) -> float:
        """Best current figure: the exact result-event cost if seen, else the
        running usage-based estimate."""
        return self._exact if self._exact is not None else self._estimated

    @property
    def is_exact(self) -> bool:
        return self._exact is not None


def booked_cost_usd(stream_path: Path, estimator: StreamCostEstimator | None = None) -> float | None:
    """The cost to record for a run that did NOT finish cleanly: the last real
    cost event in the stream if there is one, else the estimator's running
    estimate (rounded, and only if non-zero), else None. Environment knob
    for stubbed tests: none needed - pass a synthetic stream/estimator."""
    cost = last_stream_cost_usd(stream_path)
    if cost is not None:
        return cost
    if estimator is not None and estimator.cost_usd > 0:
        return round(estimator.cost_usd, 4)
    return None


# Convenience for spawn sites that keep the subprocess env: nothing else
# belongs here - permission scoping stays in each module's ALLOWED_TOOLS.
__all__ = [
    "MODEL_ROUTING",
    "MODEL_SONNET",
    "MODEL_INHERIT",
    "model_for",
    "model_args",
    "preflight_write_check",
    "last_stream_cost_usd",
    "StreamCostEstimator",
    "booked_cost_usd",
]
