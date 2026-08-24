"""
Firmware-consultant chat (POST /api/fw_chat): the chat turn for the firmware
workspace (feature spec: audits/2026-08-23-firmware-section-spec.md, section
d). Same module pattern, invocation shape, session rules, transcript
persistence, timeout and error semantics as backend/if_chat.py (the
2026-08-21 arch-chat contract) - this module only adds the firmware-specific
prompt and delegates the actual `claude` invocation to
arch_chat.run_arch_consultant with the Firmware_Coder persona (kept
re-exported here as run_fw_consultant so main.py references
fw_chat.run_fw_consultant and tests can monkeypatch the firmware chat
without touching the other chats).

A separate endpoint rather than another arch_chat view on the endpoint
because the context payload and persona differ materially: the request
carries the firmware diagram (patchable) plus the stored interface
definition as READ-ONLY context; the controller architecture is deliberately
NOT sent (the interface definition already encodes everything firmware can
reach - keeping the payload lean is a token/cost decision).

Patch contract: exactly the arch_chat diagram op set, validated against the
FIRMWARE catalog (arch_chat.validate_patch view="firmware"): kind "iface"
allowed, off-per-PHY topologies soft-warned via patch_warnings. The patch
can ONLY touch the firmware diagram; the consultant may recommend interface
changes in prose and direct the user to the interface panel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arch_chat import (  # noqa: F401 - re-exported for main.py/tests
    ArchChatResult,
    MAX_HISTORY_TURNS,
    append_turns,
    load_transcript,
    parse_reply_and_patch,
    run_arch_consultant,
    validate_session_id,
)
from arch_chat import _history_block
from firmware_blocks import ALL_FIRMWARE_BLOCK_VALUES, FIRMWARE_BLOCKS_BY_PHY

FW_KNOWLEDGE_DIR = Path.home() / ".claude" / "agent-knowledge" / "digital-microarch"


def run_fw_consultant(prompt, log_dir, log_stem, timeout_s=None):
    """Thin, monkeypatchable wrapper: same read-only consultant invocation as
    arch/if chat, but under the Firmware_Coder persona with the
    digital-microarch research notes on the read path."""
    kwargs: dict[str, Any] = {
        "agent": "Firmware_Coder",
        "knowledge_dir": FW_KNOWLEDGE_DIR,
        # Explicit Sonnet routing (invocation.MODEL_ROUTING) - agrees with,
        # but does not rely on, the charter's `model: sonnet` frontmatter.
        "run_type": "fw_chat",
    }
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    return run_arch_consultant(prompt, log_dir, log_stem, **kwargs)


def build_fw_chat_prompt(
    firmware_architecture: dict[str, Any],
    interface: dict[str, Any] | None,
    history: list[dict[str, Any]],
    message: str,
    phy_type: str | None = None,
    spec_docs_context: str | None = None,
) -> str:
    docs_block = f"{spec_docs_context}\n" if spec_docs_context else ""
    block_values = ", ".join(sorted(ALL_FIRMWARE_BLOCK_VALUES))
    phy_blocks = FIRMWARE_BLOCKS_BY_PHY.get(phy_type or "")
    phy_hint = (
        f"The usual firmware block set for a {phy_type} PHY is: "
        + ", ".join(phy_blocks)
        + ". Prefer these; stepping outside is allowed but say why.\n"
        if phy_blocks
        else ""
    )
    iface_block = (
        "## Stored interface definition (READ-ONLY context - your patch cannot touch it)\n"
        "```json\n" + json.dumps(interface, indent=2) + "\n```\n"
        "This is the SOURCE OF TRUTH for every register/signal firmware can\n"
        "reach - NEVER invent register fields that are not in this table;\n"
        "report a missing one as a finding and direct the user to the\n"
        "interface panel. You may recommend interface changes in prose, but\n"
        "your structured patch can ONLY modify the firmware diagram.\n"
        if interface
        else "## Stored interface definition\n(not provided - flag that firmware review needs it)\n"
    )

    return f"""You are acting as Firmware_Coder, an EMBEDDED-FIRMWARE ENGINEER, in an
interactive chat inside a spec-driven {phy_type or "PHY"} design tool. The
user is looking at the FIRMWARE side of the PHY: the bare-metal C software
running on the SoC that operates the controller through its CSRs - boot/init
sequencing, power-state management, calibration/training supervisors, eye
sweeps, diagnostics, and the host API.

Your engineering ground rules (state findings against them): C99, no dynamic
allocation, no OS assumptions (v1 is a bare-metal polled model - no RTOS, no
real ISR context); ALL hardware access goes through a narrow
`reg_read`/`reg_write` port layer; timing (lock waits, settle times) is
parameterized from the interface sequence values, never hard-coded.

This is a CHAT TURN, not a coding session: answer from your existing
knowledge, do at most a quick web search when you genuinely need a fact, and
keep the reply focused (a few paragraphs of markdown). Do NOT write C code
files, RTL, netlists or SPICE - firmware-architecture consultation only.

## Current FIRMWARE architecture (JSON - this is what your patch edits)
```json
{json.dumps(firmware_architecture, indent=2)}
```
Notes on the JSON: "topology" is the firmware block catalog value of each
block; topology null + kind "iface" marks a boundary anchor node. The two
iface anchors are the block's two contracts: `host-if` (up) is the host/SoC
API surface - the part the v1 register-layer codegen does NOT generate (stub
only); `csr-if` (down) is the rendered stand-in for the stored interface
definition - the controller CSRs firmware actually drives. An edge INTO the
`drv` block means "this component touches CSRs only through the driver
layer": NO supervisor may talk to `csr-if` directly - flag such an edge as a
LAYERING VIOLATION (soft warning in prose, and propose the fix).

{iface_block}
## Your review duties on every turn
- Every supervisor's targets (adapt_targets, cal targets, IRQ sources) must
  exist as signals in the interface definition - name any that don't.
- Timing fields (boot/wake/training budgets, poll timeouts) must be
  consistent with the interface reset/power sequence waits (e.g. a boot
  budget below the summed sequence timeouts cannot be met).
- Every CSR-touching block must reach `csr-if` only via `drv` (layering).
- Polled vs IRQ dispatch must be consistent with which status signals
  actually exist in the interface table.

{docs_block}{_history_block(history)}

## User's new message
{message}

## How to respond
1. Answer with real firmware-engineering substance: sequencing, polling vs
   IRQ tradeoffs, supervisor ownership of trim targets, timeout policy,
   host-API surface design.
2. ONLY if the user is asking for a change to the FIRMWARE architecture, end
   your reply with EXACTLY ONE fenced ```json block proposing the edit:
   {{"ops": [ ... ]}}
   where each op is one of:
   - {{"op": "add_block", "id": "<new-unique-id>", "label": "<human label>", "topology": <firmware block or null>, "col": <int, optional>, "row": <int, optional>, "kind": "iface" (only for boundary anchors, with topology null)}}
   - {{"op": "remove_block", "id": "<existing id>"}}   (its connections are removed too)
   - {{"op": "rename_block", "id": "<existing id>", "label": "<new label>"}}
   - {{"op": "set_topology", "id": "<existing id>", "topology": <firmware block or null>}}
   - {{"op": "add_connection", "from": "<block id>", "to": "<block id>"}}
   - {{"op": "remove_connection", "from": "<block id>", "to": "<block id>"}}
   Ops are applied in order (an add_block id may be referenced by later
   ops). "topology" must be null or exactly one of: {block_values}.
   {phy_hint}Block ids you reference must exist in the architecture above (or be
   introduced by an earlier add_block in the same patch). Do NOT emit a
   patch for a purely informational question - just answer it.
"""
