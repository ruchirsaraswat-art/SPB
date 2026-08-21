"""
Isolated module for invoking Circuit_Researcher headlessly via the `claude` CLI.

Mirrors circuit_builder.py's isolation contract: this is the ONLY place that
knows about the `claude -p ...` invocation for Circuit_Researcher. Reuses the
same validated invocation contract (--agent, --output-format stream-json
--verbose, narrow --allowedTools instead of a bypass flag) documented in
circuit_builder.py's module docstring - see that file for why.

One deliberate difference from circuit_builder's tool scoping: Circuit_Researcher's
whole point is to maintain a persistent, self-improving knowledge base on disk
at ~/.claude/agent-knowledge/circuit-topologies/ (see its agent definition).
That is a legitimate write target OUTSIDE the run directory, unlike
Circuit_Builder (which has no business writing anywhere but its run dir), so
the allow-list here explicitly grants Write/Edit on that one knowledge-base
path in addition to the run directory - not a bare Write/Edit, and not
anywhere else.
"""

from __future__ import annotations

import json
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from topologies import format_extra_field_lines, topology_display_name

CLAUDE_BIN = "claude"
KNOWLEDGE_BASE_DIR = Path.home() / ".claude" / "agent-knowledge" / "circuit-topologies"

ALLOWED_TOOLS = [
    "WebSearch",
    "WebFetch",
    "Read",
    "Glob",
    "Grep",
    "Write(**)",  # scoped to cwd (the research run directory) via relative glob
    "Edit(**)",
    f"Write({KNOWLEDGE_BASE_DIR}/**)",  # its persistent knowledge base - a legitimate out-of-cwd write target
    f"Edit({KNOWLEDGE_BASE_DIR}/**)",
    "Bash(ls *)",
    "Bash(ls)",
    "Bash(cat *)",
    "Bash(mkdir *)",
    "Bash(find *)",
    "Bash(grep *)",
    "Bash(pwd)",
]

DEFAULT_TIMEOUT_S = 900  # research is web-search-bound, not sim-bound; should be much faster than a build run
POLL_INTERVAL_S = 1.0


@dataclass
class CircuitResearcherResult:
    status: str  # "success" | "failed"
    reason: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None


def build_prompt(spec: dict[str, Any]) -> str:
    category = spec.get("topology", "")
    display_name = topology_display_name(category, spec.get("custom_topology"))
    phy_type = spec.get("phy_type", "ser-des").upper()
    selected_block = spec.get("selected_block", "")

    def line(label: str, value: Any, unit: str = "") -> str:
        if value is None or value == "":
            return f"- {label}: not specified"
        return f"- {label}: {value}{(' ' + unit) if unit else ''}"

    # Topology-specific constraint fields (see topologies.TOPOLOGY_FIELDS) -
    # same field set/units the build step's prompt uses, so a candidate
    # Circuit_Researcher recommends is being compared against the exact
    # numbers Circuit_Builder will later be asked to hit.
    extra_field_lines = format_extra_field_lines(category, spec.get("extra_fields"))
    extra_fields_block = (
        "\n".join(extra_field_lines)
        if extra_field_lines
        else "- (no additional structured spec fields for this topology)"
    )

    return f"""Research and compare topology options for a {display_name} - this is a
{phy_type} building block (block: {selected_block if selected_block else "user-selected"}) - under the constraints
below, and recommend one with rationale. This is a research/recommendation
task only: do NOT write any netlist, schematic, or SPICE testbench, and do
NOT invoke xschem/ngspice/magic. Follow your usual workflow (read your
knowledge base INDEX first, do fresh research as needed, update your notes
afterward).

## Constraints
{extra_fields_block}
{line("Power budget", spec.get("power_mw"), "mW")}
{line("Area budget", spec.get("area_um2"), "um^2")}
{line("Supply voltage", spec.get("vdd_v"), "V")}
{line("Simulation temperature", spec.get("temp_c"), "C")}
{line("Process corner", spec.get("corner"), "(sky130 tt/ff/ss/sf/fs)")}
{line("Additional notes from the user", spec.get("notes"))}

Process target is sky130 (open-source PDK) - flag sky130-specific realities
(limited Vt options, headroom at low supply, available device types) where
relevant, per your usual practice.

As the LAST thing you do, write a file named exactly `research_summary.json`
in the CURRENT WORKING DIRECTORY (via the Write tool) containing ONLY a
single JSON object (no markdown fence, no extra text in that file) with
exactly these keys:
{{
  "status": "success" or "failed",
  "error": "" or a short plain-English reason if status is "failed",
  "category": "{category}",
  "category_label": "{display_name}",
  "candidates": [
    {{"name": "<short architecture name, e.g. 'passive single-zero RC CTLE'>",
      "summary": "<1-3 sentence description>",
      "pros": ["<short phrase>", "..."],
      "cons": ["<short phrase>", "..."]}},
    ... (2-3 candidates actually in contention - not an exhaustive survey)
  ],
  "recommended": "<must exactly match one candidate's \\"name\\" above>",
  "rationale": "<why the recommended one wins for THESE constraints, a few sentences>",
  "notes": "<caveats, confidence level, anything you couldn't verify or that needs a real spec to decide>"
}}
Write this file even if something failed along the way - in that case set
status to "failed", fill in "error", and leave "candidates" as an empty list
and "recommended"/"rationale" as empty strings. This file is the single
source of truth another program will read to let a user pick a topology, so
it MUST exist and MUST be valid JSON parseable on its own.

After writing research_summary.json, give a brief (3-5 sentence) plain-English
summary of your recommendation as your final reply.
"""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _format_event(line: str) -> str | None:
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return f"[raw] {line}"

    etype = ev.get("type")
    if etype == "system" and ev.get("subtype") == "init":
        return f"[session] started (model={ev.get('model')}, agent=Circuit_Researcher)"
    if etype == "assistant":
        parts = []
        for block in ev.get("message", {}).get("content", []) or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                parts.append(f"[assistant] {block['text']}")
            elif btype == "thinking" and block.get("thinking"):
                snippet = block["thinking"].strip().replace("\n", " ")
                if len(snippet) > 300:
                    snippet = snippet[:300] + "..."
                parts.append(f"[thinking] {snippet}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {}) or {}
                if name == "WebSearch":
                    detail = inp.get("query", "")
                elif name == "WebFetch":
                    detail = inp.get("url", "")
                elif name == "Bash":
                    detail = inp.get("command", "")
                elif name in ("Write", "Edit"):
                    detail = inp.get("file_path", "")
                elif name == "Read":
                    detail = inp.get("file_path", "")
                else:
                    detail = json.dumps(inp)[:200]
                parts.append(f"[tool] {name}: {detail}")
        return "\n".join(parts) if parts else None
    if etype == "user":
        for block in ev.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content)
                text = text.strip().replace("\n", " ")
                if len(text) > 250:
                    text = text[:250] + "..."
                return f"[tool result] {text}"
        return None
    if etype == "result":
        return (
            f"[session] finished: is_error={ev.get('is_error')} "
            f"cost=${ev.get('total_cost_usd', 0):.4f} "
            f"duration={ev.get('duration_ms', 0)}ms"
        )
    return None


def run_circuit_researcher(
    spec: dict[str, Any],
    run_dir: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
) -> CircuitResearcherResult:
    """
    Invoke Circuit_Researcher headlessly to compare topology candidates for
    `spec`'s topology category and recommend one, inside `run_dir` (must
    already exist, one directory per research request).

    Streams progress into run_dir/session.log and run_dir/claude_stream.jsonl
    exactly like run_circuit_builder does, so the same LogPanel component can
    tail either kind of run.

    Never raises for ordinary failure modes; reports status="failed" instead.
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_prompt(spec)
    (run_dir / "prompt.txt").write_text(prompt)

    cmd = [CLAUDE_BIN, "--agent", "Circuit_Researcher", "--add-dir", str(KNOWLEDGE_BASE_DIR)]
    for tool in ALLOWED_TOOLS:
        cmd += ["--allowedTools", tool]
    cmd += ["--output-format", "stream-json", "--verbose", "-p", prompt]

    human_log_path = run_dir / "session.log"
    raw_log_path = run_dir / "claude_stream.jsonl"

    started = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(run_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    final_event: dict[str, Any] | None = None
    cancelled = False
    timed_out = False

    with open(human_log_path, "w") as human_f, open(raw_log_path, "w") as raw_f:
        human_f.write(f"[session] launching Circuit_Researcher for run in {run_dir}\n")
        human_f.flush()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.terminate()
                break
            if time.time() - started > timeout_s:
                timed_out = True
                proc.terminate()
                break
            ready, _, _ = select.select([proc.stdout], [], [], POLL_INTERVAL_S)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if line == "":
                break
            raw_f.write(line)
            raw_f.flush()
            try:
                ev = json.loads(line)
                if ev.get("type") == "result":
                    final_event = ev
            except json.JSONDecodeError:
                pass
            formatted = _format_event(line)
            if formatted:
                human_f.write(formatted + "\n")
                human_f.flush()

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

        if cancelled:
            human_f.write("[session] cancelled by user\n")
        elif timed_out:
            human_f.write(f"[session] timed out after {timeout_s}s\n")

    duration_ms = int((time.time() - started) * 1000)

    if cancelled:
        return CircuitResearcherResult(status="failed", reason="Cancelled by user", duration_ms=duration_ms)
    if timed_out:
        return CircuitResearcherResult(
            status="failed",
            reason=f"Circuit_Researcher invocation timed out after {timeout_s}s",
            duration_ms=duration_ms,
        )
    if proc.returncode != 0:
        return CircuitResearcherResult(
            status="failed",
            reason=f"claude CLI exited with code {proc.returncode}",
            duration_ms=duration_ms,
        )
    if final_event is None:
        return CircuitResearcherResult(
            status="failed",
            reason="claude CLI exited without emitting a final result event",
            duration_ms=duration_ms,
        )

    raw_text = final_event.get("result", "") or ""
    session_id = final_event.get("session_id")
    cost_usd = final_event.get("total_cost_usd")

    if final_event.get("is_error"):
        return CircuitResearcherResult(
            status="failed",
            reason="Circuit_Researcher session reported an error",
            raw_text=raw_text,
            session_id=session_id,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    summary_path = run_dir / "research_summary.json"
    summary: dict[str, Any] | None = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            summary = None

    if summary is None:
        summary = _extract_json_object(raw_text)

    if summary is None:
        return CircuitResearcherResult(
            status="failed",
            reason="Circuit_Researcher did not produce a parseable research_summary.json (or JSON in its reply)",
            raw_text=raw_text,
            session_id=session_id,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )

    status = summary.get("status", "failed")
    reason = summary.get("error", "") or ""
    candidates = summary.get("candidates") or []
    if status == "success" and not candidates:
        status = "failed"
        reason = reason or "research_summary.json claimed success but listed no candidates"

    return CircuitResearcherResult(
        status=status,
        reason=reason,
        summary=summary,
        raw_text=raw_text,
        session_id=session_id,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )
