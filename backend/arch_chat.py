"""
Architecture-discussion chat: isolated module for invoking a headless
Circuit_Researcher-style "architecture consultant" session via the `claude`
CLI, plus the patch contract that lets its answers propose structured edits
to the currently displayed PHY architecture.

Mirrors circuit_researcher.py's isolation contract: this is the ONLY place
that knows about the `claude -p ...` invocation for architecture chat. Same
validated invocation shape (--agent Circuit_Researcher, --output-format
stream-json --verbose, narrow --allowedTools) - see circuit_builder.py's
module docstring for why that shape.

Deliberate differences from circuit_researcher.py, both cost-control:
  - READ-ONLY tool allowlist (WebSearch/WebFetch/Read/Glob/Grep on the
    knowledge base). A chat turn is an interactive, paid invocation; it
    should answer from knowledge + at most a quick search, not run a full
    research campaign or update the knowledge base.
  - Shorter timeout (ARCH_CHAT_TIMEOUT_S) and a bounded prompt: the current
    architecture JSON + only the last MAX_HISTORY_TURNS transcript turns,
    never a full history dump.

# Patch contract (the exact schema the consultant must emit and the frontend
# applies - keep this comment in sync with audits/2026-08-21-arch-chat-feature.md)
The consultant MAY end its reply with ONE fenced ```json block containing:
    {"ops": [ <op>, ... ]}
where each <op> is one of:
  {"op": "add_block", "id": str, "label": str, "topology": str|null,
   "col": int (optional), "row": int (optional), "kind": "pad" (optional)}
  {"op": "remove_block", "id": str}          # implies removing its edges
  {"op": "rename_block", "id": str, "label": str}
  {"op": "set_topology", "id": str, "topology": str|null}
  {"op": "add_connection", "from": str, "to": str}
  {"op": "remove_connection", "from": str, "to": str}
Validation (validate_patch) applies ops IN ORDER against a working copy of
the architecture, so add_block followed by add_connection to the new id is
valid. Topology values must be in topologies.ALL_TOPOLOGY_VALUES (imported -
single source of truth) or null (non-designable node: pad, photodiode...).
Invalid ops get an "error" field; valid ops are still applied to the working
copy so later ops are judged against the best-effort state.
"""

from __future__ import annotations

import json
import re
import select
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digital_blocks import ALL_DIGITAL_BLOCK_VALUES, DIGITAL_BLOCKS_BY_PHY
from firmware_blocks import ALL_FIRMWARE_BLOCK_VALUES, FIRMWARE_BLOCKS_BY_PHY
from topologies import ALL_TOPOLOGY_VALUES

CLAUDE_BIN = "claude"
KNOWLEDGE_BASE_DIR = Path.home() / ".claude" / "agent-knowledge" / "circuit-topologies"

# Read-only: chat must not mutate the knowledge base or the filesystem.
ALLOWED_TOOLS = [
    "WebSearch",
    "WebFetch",
    "Read",
    "Glob",
    "Grep",
]

ARCH_CHAT_TIMEOUT_S = 300  # one chat turn, not a research campaign
POLL_INTERVAL_S = 1.0
MAX_HISTORY_TURNS = 8  # last 8 turns (4 user/assistant exchanges) go in the prompt

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

VALID_OPS = {
    "add_block",
    "remove_block",
    "rename_block",
    "set_topology",
    "add_connection",
    "remove_connection",
}


def validate_session_id(session_id: str) -> str:
    """Session ids become filenames under arch_chats/ - restrict to a safe
    charset so they can't traverse paths. Raises ValueError."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            "session_id must be 1-64 characters of letters, digits, '_' or '-' "
            f"(got {session_id!r})"
        )
    return session_id


# ---------------------------------------------------------------------------
# Architecture normalization + patch validation


def normalize_architecture(arch: Any) -> dict[str, Any]:
    """Lenient normalization of the architecture JSON the frontend holds:
    {"blocks": [...], "edges": [...]} - "connections" accepted as an alias
    for "edges". Raises ValueError if the shape is unusable."""
    if not isinstance(arch, dict):
        raise ValueError("architecture must be a JSON object with 'blocks' and 'edges'")
    blocks = arch.get("blocks")
    edges = arch.get("edges", arch.get("connections"))
    if not isinstance(blocks, list):
        raise ValueError("architecture.blocks must be a list of block objects")
    if edges is None:
        edges = []
    if not isinstance(edges, list):
        raise ValueError("architecture.edges (or .connections) must be a list of {from, to}")
    norm_blocks = []
    seen_ids = set()
    for b in blocks:
        if not isinstance(b, dict) or not b.get("id"):
            raise ValueError(f"every block needs an 'id' (got {b!r})")
        if b["id"] in seen_ids:
            raise ValueError(f"duplicate block id {b['id']!r} in architecture")
        seen_ids.add(b["id"])
        norm_blocks.append(dict(b))
    norm_edges = []
    for e in edges:
        if not isinstance(e, dict) or not e.get("from") or not e.get("to"):
            raise ValueError(f"every edge needs 'from' and 'to' block ids (got {e!r})")
        norm_edges.append({"from": e["from"], "to": e["to"]})
    return {"blocks": norm_blocks, "edges": norm_edges}


# Per-view catalog + non-designable node kind (2026-08-23 digital-arch spec;
# "firmware" added by the 2026-08-23 firmware-section spec): the "afe" view
# validates topologies against the analog catalog and allows kind:"pad"
# anchor nodes; the "digital" and "firmware" views validate against their own
# block catalogs and allow kind:"iface" boundary anchors (AFE side / core
# side / host API / controller CSRs) - drawn, wireable, not selectable,
# exactly like pads. The firmware view is reached via POST /api/fw_chat
# (backend/fw_chat.py), not another arch_chat view value on the endpoint.
VALID_VIEWS = ("afe", "digital", "firmware")
_VIEW_CATALOGS = {
    "afe": (ALL_TOPOLOGY_VALUES, "topology"),
    "digital": (ALL_DIGITAL_BLOCK_VALUES, "digital block"),
    "firmware": (ALL_FIRMWARE_BLOCK_VALUES, "firmware block"),
}
_VIEW_KINDS = {"afe": "pad", "digital": "iface", "firmware": "iface"}
# Per-PHY "usual set" for the soft add_block warning (None = no such check).
_VIEW_BY_PHY = {"digital": DIGITAL_BLOCKS_BY_PHY, "firmware": FIRMWARE_BLOCKS_BY_PHY}


def _check_topology_value(value: Any, view: str = "afe") -> str | None:
    """None is legal (non-designable node: pad, photodiode, iface anchor).
    Anything else must be a known value of the view's catalog."""
    if value is None:
        return None
    allowed, noun = _VIEW_CATALOGS[view]
    if not isinstance(value, str) or value not in allowed:
        return (
            f"unknown {noun} {value!r} - must be null or one of: "
            + ", ".join(sorted(allowed))
        )
    return None


def validate_patch(
    patch: Any,
    architecture: dict[str, Any],
    view: str = "afe",
    phy_type: str | None = None,
) -> tuple[list[dict], bool]:
    """Validate {"ops": [...]} against a normalized architecture. Returns
    (annotated_ops, all_valid): each returned op is a copy of the input op,
    with an "error" string added when invalid. Ops are applied in order to a
    working copy (valid ones only), so an add_block followed by an
    add_connection referencing the new id validates. Never raises for bad op
    content - a malformed patch is a per-op error, not a chat failure.

    `view` selects the topology catalog ("afe" = analog topologies, the
    default/pre-existing behavior; "digital" = the digital block catalog) and
    the legal non-designable `kind` ("pad" vs "iface"). For the digital view,
    an add_block whose topology is outside DIGITAL_BLOCKS_BY_PHY[phy_type]
    gets a "warning" annotation - soft by design (users may legitimately
    borrow blocks across PHY families), never a rejection."""
    ops_in = patch.get("ops") if isinstance(patch, dict) else None
    if not isinstance(ops_in, list):
        return (
            [{"op": "invalid", "error": 'patch must be an object {"ops": [...]}'}],
            False,
        )

    block_ids = {b["id"] for b in architecture["blocks"]}
    edges = {(e["from"], e["to"]) for e in architecture["edges"]}
    annotated: list[dict] = []
    all_valid = True

    allowed_kind = _VIEW_KINDS[view]
    by_phy = _VIEW_BY_PHY.get(view)
    phy_blocks = by_phy.get(phy_type or "") if by_phy is not None else None

    for raw in ops_in:
        op = dict(raw) if isinstance(raw, dict) else {"op": repr(raw)}
        error = None
        kind = op.get("op")
        if kind not in VALID_OPS:
            error = f"unknown op {kind!r} - must be one of: {', '.join(sorted(VALID_OPS))}"
        elif kind == "add_block":
            bid = op.get("id")
            if not bid or not isinstance(bid, str):
                error = "add_block requires a non-empty string 'id'"
            elif bid in block_ids:
                error = f"add_block: block id {bid!r} already exists"
            elif not op.get("label") or not isinstance(op.get("label"), str):
                error = "add_block requires a non-empty string 'label'"
            elif op.get("kind") is not None and op.get("kind") != allowed_kind:
                error = (
                    f"add_block: kind {op.get('kind')!r} is not valid in the {view} view "
                    f"- only {allowed_kind!r} (or omit it)"
                )
            else:
                error = _check_topology_value(op.get("topology"), view)
            if error is None:
                block_ids.add(bid)
                # Soft per-PHY applicability check (digital view only).
                topo = op.get("topology")
                if phy_blocks is not None and topo is not None and topo not in phy_blocks:
                    op["warning"] = (
                        f"{topo!r} is not in the usual {view} block set for a "
                        f"{phy_type} PHY - allowed, but double-check it belongs here."
                    )
        elif kind == "remove_block":
            bid = op.get("id")
            if bid not in block_ids:
                error = f"remove_block: no block with id {bid!r}"
            else:
                block_ids.discard(bid)
                # Removing a block removes every edge touching it - the UI
                # must apply the same rule.
                edges = {e for e in edges if bid not in e}
        elif kind == "rename_block":
            bid = op.get("id")
            if bid not in block_ids:
                error = f"rename_block: no block with id {bid!r}"
            elif not op.get("label") or not isinstance(op.get("label"), str):
                error = "rename_block requires a non-empty string 'label'"
        elif kind == "set_topology":
            bid = op.get("id")
            if bid not in block_ids:
                error = f"set_topology: no block with id {bid!r}"
            else:
                error = _check_topology_value(op.get("topology"), view)
        elif kind == "add_connection":
            src, dst = op.get("from"), op.get("to")
            if src not in block_ids:
                error = f"add_connection: no block with id {src!r} (from)"
            elif dst not in block_ids:
                error = f"add_connection: no block with id {dst!r} (to)"
            elif src == dst:
                error = "add_connection: from and to must differ"
            elif (src, dst) in edges:
                error = f"add_connection: connection {src!r} -> {dst!r} already exists"
            else:
                edges.add((src, dst))
        elif kind == "remove_connection":
            src, dst = op.get("from"), op.get("to")
            if (src, dst) not in edges:
                error = f"remove_connection: no connection {src!r} -> {dst!r}"
            else:
                edges.discard((src, dst))

        if error:
            op["error"] = error
            all_valid = False
        annotated.append(op)

    return annotated, all_valid


# ---------------------------------------------------------------------------
# Reply parsing: markdown text + optional trailing fenced JSON patch


def parse_reply_and_patch(raw_text: str) -> tuple[str, dict | None]:
    """Split the consultant's final message into (reply_markdown, patch).
    The patch is the LAST fenced ```json block whose content parses as an
    object with an "ops" list; that block is stripped from the reply. Any
    malformed/absent patch -> (full text, None). Never raises."""
    if not raw_text:
        return "", None
    matches = list(re.finditer(r"```json\s*\n(.*?)\n?\s*```", raw_text, re.DOTALL))
    for match in reversed(matches):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ops"), list):
            reply = (raw_text[: match.start()] + raw_text[match.end() :]).strip()
            return reply, obj
    return raw_text.strip(), None


# ---------------------------------------------------------------------------
# Transcript persistence: one JSON file per session under arch_chats/


def transcript_path(root: Path, session_id: str) -> Path:
    return root / f"{validate_session_id(session_id)}.json"


def load_transcript(root: Path, session_id: str) -> dict[str, Any]:
    path = transcript_path(root, session_id)
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("turns"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "turns": [],
    }


def append_turns(root: Path, session_id: str, *turns: dict[str, Any]) -> dict[str, Any]:
    data = load_transcript(root, session_id)
    data["turns"].extend(turns)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = transcript_path(root, session_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    return data


# ---------------------------------------------------------------------------
# Prompt


def _history_block(history: list[dict[str, Any]]) -> str:
    recent = history[-MAX_HISTORY_TURNS:]
    if recent:
        history_lines = "\n".join(
            f"[{t.get('role', '?')}] {t.get('content', '')}" for t in recent
        )
        return f"## Conversation so far (last {len(recent)} turns)\n{history_lines}"
    return "## Conversation so far\n(none - this is the first message)"


def build_chat_prompt(
    architecture: dict[str, Any],
    history: list[dict[str, Any]],
    message: str,
    phy_type: str | None = None,
    view: str = "afe",
    afe_architecture: dict[str, Any] | None = None,
    spec_docs_context: str | None = None,
) -> str:
    """Prompt for one consultant turn. `view="digital"` frames the chat as a
    digital-microarchitecture discussion: patch topologies come from the
    digital block catalog, kind "iface" marks boundary anchors, and the AFE
    architecture (when supplied) is included as READ-ONLY context so the
    consultant keeps the two sides consistent. `spec_docs_context` (2026-08-23
    rtl-gen spec, section a) is the pre-rendered compact spec-document
    metadata block (see spec_docs.render_chat_context) - digital view only."""
    if view == "digital":
        return _build_digital_chat_prompt(
            architecture, history, message, phy_type, afe_architecture,
            spec_docs_context=spec_docs_context,
        )

    topo_values = ", ".join(sorted(ALL_TOPOLOGY_VALUES))

    return f"""You are acting as an analog/mixed-signal PHY ARCHITECTURE CONSULTANT in an
interactive chat inside a spec-driven design tool. The user is looking at a
block-level {phy_type or "PHY"} architecture diagram (blocks + directed
signal-flow connections) and wants to discuss it with you.

This is a CHAT TURN, not a research campaign: answer from your existing
topology knowledge (read your knowledge base INDEX if helpful), do at most a
quick web search when you genuinely need a fact, and keep the reply focused
(a few paragraphs of markdown). Do NOT write any files, netlists, schematics
or SPICE - architecture-level consultation only.

## Current architecture (JSON)
```json
{json.dumps(architecture, indent=2)}
```
Notes on the JSON: "topology" is the backend topology category of each block
(null = a node this flow can't design: pad, photodiode, digital cal logic);
"edges" are directed signal-flow connections between block ids.

{_history_block(history)}

## User's new message
{message}

## How to respond
1. Answer the question with real topology-research substance: tradeoffs,
   standard practice in production PHYs, and sky130 open-PDK realities
   (headroom at 1.8 V, realistic speeds) where relevant.
2. ONLY if the user is asking for a change to the architecture, end your
   reply with EXACTLY ONE fenced ```json block proposing the edit:
   {{"ops": [ ... ]}}
   where each op is one of:
   - {{"op": "add_block", "id": "<new-unique-id>", "label": "<human label>", "topology": <topology or null>, "col": <int, optional>, "row": <int, optional>}}
   - {{"op": "remove_block", "id": "<existing id>"}}   (its connections are removed too)
   - {{"op": "rename_block", "id": "<existing id>", "label": "<new label>"}}
   - {{"op": "set_topology", "id": "<existing id>", "topology": <topology or null>}}
   - {{"op": "add_connection", "from": "<block id>", "to": "<block id>"}}
   - {{"op": "remove_connection", "from": "<block id>", "to": "<block id>"}}
   Ops are applied in order (an add_block id may be referenced by later
   ops). "topology" must be null or exactly one of: {topo_values}.
   Block ids you reference must exist in the architecture above (or be
   introduced by an earlier add_block in the same patch). Do NOT emit a
   patch for a purely informational question - just answer it.
"""


def _build_digital_chat_prompt(
    architecture: dict[str, Any],
    history: list[dict[str, Any]],
    message: str,
    phy_type: str | None,
    afe_architecture: dict[str, Any] | None,
    spec_docs_context: str | None = None,
) -> str:
    docs_block = f"{spec_docs_context}\n" if spec_docs_context else ""
    block_values = ", ".join(sorted(ALL_DIGITAL_BLOCK_VALUES))
    phy_blocks = DIGITAL_BLOCKS_BY_PHY.get(phy_type or "")
    phy_hint = (
        f"The usual digital block set for a {phy_type} PHY is: "
        + ", ".join(phy_blocks)
        + ". Prefer these; stepping outside is allowed but say why.\n"
        if phy_blocks
        else ""
    )
    afe_block = (
        "## AFE architecture (READ-ONLY context - do NOT patch it)\n"
        "```json\n" + json.dumps(afe_architecture, indent=2) + "\n```\n"
        "Keep the digital architecture consistent with this AFE side (e.g. the "
        "deserializer's parallel width feeds the word aligner; training/cal "
        "blocks own the AFE trim knobs). You may recommend AFE changes in "
        "prose, but your patch can only touch the digital diagram.\n"
        if afe_architecture
        else ""
    )

    return f"""You are acting as a DIGITAL MICROARCHITECTURE CONSULTANT in an interactive
chat inside a spec-driven PHY design tool. The user is looking at the DIGITAL
side of a {phy_type or "PHY"}: the synchronous logic between the AFE
(deserializer/serializer, trim/status signals) and the SoC core - aligners,
FIFOs, codecs, CSRs, training/cal/power FSMs.

This is a CHAT TURN, not a research campaign: answer from your existing
knowledge, do at most a quick web search when you genuinely need a fact, and
keep the reply focused (a few paragraphs of markdown). Do NOT write RTL,
netlists, schematics or SPICE - architecture-level consultation only.

sky130 realities to respect: line rates ~1-2 Gb/s; digital word clocks
50-200 MHz (sky130 std-cell designs routinely close ~100 MHz) - warn when a
proposal exceeds that.

## Current DIGITAL architecture (JSON)
```json
{json.dumps(architecture, indent=2)}
```
Notes on the JSON: "topology" is the digital block catalog value of each
block; topology null + kind "iface" marks a boundary anchor node (AFE side,
core side, host bus) - drawn and wireable but not designable. "edges" are
directed signal-flow connections between block ids.

{docs_block}{afe_block}{_history_block(history)}

## User's new message
{message}

## How to respond
1. Answer with real microarchitecture substance: CDC discipline, elastic
   buffering, training/cal ownership of AFE knobs, standard practice in
   production PHY digital, and sky130 synthesized-logic realities.
2. ONLY if the user is asking for a change to the DIGITAL architecture, end
   your reply with EXACTLY ONE fenced ```json block proposing the edit:
   {{"ops": [ ... ]}}
   where each op is one of:
   - {{"op": "add_block", "id": "<new-unique-id>", "label": "<human label>", "topology": <digital block or null>, "col": <int, optional>, "row": <int, optional>, "kind": "iface" (only for boundary anchors, with topology null)}}
   - {{"op": "remove_block", "id": "<existing id>"}}   (its connections are removed too)
   - {{"op": "rename_block", "id": "<existing id>", "label": "<new label>"}}
   - {{"op": "set_topology", "id": "<existing id>", "topology": <digital block or null>}}
   - {{"op": "add_connection", "from": "<block id>", "to": "<block id>"}}
   - {{"op": "remove_connection", "from": "<block id>", "to": "<block id>"}}
   Ops are applied in order (an add_block id may be referenced by later
   ops). "topology" must be null or exactly one of: {block_values}.
   {phy_hint}Block ids you reference must exist in the architecture above (or be
   introduced by an earlier add_block in the same patch). Do NOT emit a
   patch for a purely informational question - just answer it.
"""


# ---------------------------------------------------------------------------
# Headless invocation


@dataclass
class ArchChatResult:
    status: str  # "success" | "failed"
    reason: str = ""
    raw_text: str = ""
    session_id: str | None = None  # claude CLI session id (not our chat session)
    cost_usd: float | None = None
    duration_ms: int | None = None


def run_arch_consultant(
    prompt: str,
    log_dir: Path,
    log_stem: str,
    timeout_s: int = ARCH_CHAT_TIMEOUT_S,
    agent: str = "Circuit_Researcher",
    knowledge_dir: Path | None = None,
) -> ArchChatResult:
    """One synchronous consultant turn: `claude --agent <agent> -p <prompt>`
    with a read-only tool allowlist. Writes <log_stem>.log (human readable)
    and <log_stem>.stream.jsonl (raw events) into log_dir for debugging.
    Never raises for ordinary failures; returns status="failed".

    `agent`/`knowledge_dir` default to the pre-existing architecture-chat
    behavior (Circuit_Researcher + the circuit-topologies knowledge base);
    fw_chat passes Firmware_Coder + the digital-microarch notes instead -
    same invocation contract, different persona.

    Module-level by design so main.py references arch_chat.run_arch_consultant
    and tests can monkeypatch it without touching the endpoint code."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{log_stem}.prompt.txt").write_text(prompt)

    cmd = [CLAUDE_BIN, "--agent", agent, "--add-dir", str(knowledge_dir or KNOWLEDGE_BASE_DIR)]
    for tool in ALLOWED_TOOLS:
        cmd += ["--allowedTools", tool]
    cmd += ["--output-format", "stream-json", "--verbose", "-p", prompt]

    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(log_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return ArchChatResult(status="failed", reason=f"`{CLAUDE_BIN}` CLI not found on PATH")

    final_event: dict[str, Any] | None = None
    timed_out = False

    with open(log_dir / f"{log_stem}.stream.jsonl", "w") as raw_f:
        while True:
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

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    duration_ms = int((time.time() - started) * 1000)

    if timed_out:
        return ArchChatResult(
            status="failed",
            reason=f"consultant invocation timed out after {timeout_s}s",
            duration_ms=duration_ms,
        )
    if proc.returncode != 0:
        return ArchChatResult(
            status="failed",
            reason=f"claude CLI exited with code {proc.returncode}",
            duration_ms=duration_ms,
        )
    if final_event is None:
        return ArchChatResult(
            status="failed",
            reason="claude CLI exited without emitting a final result event",
            duration_ms=duration_ms,
        )
    if final_event.get("is_error"):
        return ArchChatResult(
            status="failed",
            reason="consultant session reported an error",
            raw_text=final_event.get("result", "") or "",
            session_id=final_event.get("session_id"),
            cost_usd=final_event.get("total_cost_usd"),
            duration_ms=duration_ms,
        )
    return ArchChatResult(
        status="success",
        raw_text=final_event.get("result", "") or "",
        session_id=final_event.get("session_id"),
        cost_usd=final_event.get("total_cost_usd"),
        duration_ms=duration_ms,
    )
