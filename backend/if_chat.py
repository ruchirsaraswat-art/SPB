"""
Interface-consultant chat (POST /api/if_chat): the chat turn for the
AFE<->digital interface workspace. Same module pattern, invocation shape,
session rules, transcript persistence, timeout and error semantics as
backend/arch_chat.py (the 2026-08-21 contract) - this module only adds the
interface-specific prompt and delegates the actual `claude` invocation to
arch_chat.run_arch_consultant (kept re-exported here as run_if_consultant so
main.py references if_chat.run_if_consultant and tests can monkeypatch the
interface chat without touching the architecture chat).

Patch contract: interfaces.apply_if_patch's 10 ops (add/remove/update signal,
add/remove/update clock domain, add/remove cdc, set reset/power sequence) -
see audits/2026-08-23-digital-arch-spec.md section c. Both architectures in
the request are READ-ONLY context: the consultant may recommend arch changes
in prose but its patch can only touch the interface.
"""

from __future__ import annotations

import json
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
from interfaces import DIRECTIONS, HANDSHAKES, RATES, SIGNAL_GROUPS


def run_if_consultant(prompt, log_dir, log_stem, timeout_s=None):
    """Thin, monkeypatchable wrapper over the shared consultant invocation."""
    kwargs = {} if timeout_s is None else {"timeout_s": timeout_s}
    return run_arch_consultant(prompt, log_dir, log_stem, **kwargs)


def build_if_chat_prompt(
    interface: dict[str, Any],
    afe_architecture: dict[str, Any] | None,
    digital_architecture: dict[str, Any] | None,
    history: list[dict[str, Any]],
    message: str,
    phy_type: str | None = None,
    spec_docs_context: str | None = None,
) -> str:
    docs_block = f"{spec_docs_context}\n" if spec_docs_context else ""

    def arch_block(title: str, arch: dict[str, Any] | None) -> str:
        if not arch:
            return f"## {title}\n(not provided)\n"
        return f"## {title}\n```json\n{json.dumps(arch, indent=2)}\n```\n"

    return f"""You are acting as an AFE<->DIGITAL INTERFACE CONSULTANT in an interactive
chat inside a spec-driven {phy_type or "PHY"} design tool. The user is editing
a structured interface definition - the signal-table contract between the
analog front end (AFE) diagram and the digital-architecture diagram: clock
domains, signals, CDC points, and the reset/power sequences.

This is a CHAT TURN, not a research campaign: answer from your existing
knowledge, do at most a quick web search when you genuinely need a fact, and
keep the reply focused (a few paragraphs of markdown). Do NOT write RTL,
netlists, schematics or SPICE.

sky130 realities to respect: line rates ~1-2 Gb/s; digital word clocks
50-200 MHz (warn above ~200 MHz for synthesized logic).

## Current interface definition (JSON - this is what your patch edits)
```json
{json.dumps(interface, indent=2)}
```

{arch_block("AFE architecture (READ-ONLY context - your patch cannot touch it)", afe_architecture)}
{arch_block("Digital architecture (READ-ONLY context - your patch cannot touch it)", digital_architecture)}
Both architectures are READ-ONLY: you may recommend architecture changes in
prose, but your structured patch can ONLY modify the interface definition.

## Field vocabularies (server-enforced)
- direction: {" | ".join(DIRECTIONS)}  (a2d = AFE->digital, d2a = digital->AFE, ext = board/SoC-sourced)
- rate: {" | ".join(RATES)}  (line-rate signals should NOT cross the boundary - warn if one does; rate_mhz is required for "word")
- group: {" | ".join(SIGNAL_GROUPS)}
- handshake: {" | ".join(HANDSHAKES)}
- clock_domain: a defined clock_domains name, or "async"
- name: [a-z][a-z0-9_]*, unique; width: integer >= 1

## Your review duties on every turn
- Every a2d/d2a signal should be accounted for by some AFE-block/digital-block
  pair (afe_block/digital_block ids from the two diagrams).
- Every asynchronous crossing needs a cdc_points entry (or handshake 2ff_sync).
- Sequences should gate on status signals (pll_locked, cdr_locked, ...) rather
  than bare waits wherever a status signal exists.
- No line-rate signal crosses the boundary.

{docs_block}{_history_block(history)}

## User's new message
{message}

## How to respond
1. Answer with real interface-engineering substance: CDC strategy, hold rules
   for quasi-static trim buses, sequencing, handshake choices.
2. ONLY if the user is asking for a change to the interface definition, end
   your reply with EXACTLY ONE fenced ```json block proposing the edit:
   {{"ops": [ ... ]}}
   where each op is one of (applied IN ORDER - an add_signal name may be
   referenced by a later add_cdc in the same patch):
   - {{"op": "add_signal", "signal": {{ ...complete signal record... }}}}
   - {{"op": "remove_signal", "name": "<existing signal name>"}}   (its cdc entries are removed too)
   - {{"op": "update_signal", "name": "<existing>", "fields": {{ ...partial record; rename via "name" in fields... }}}}
   - {{"op": "add_clock_domain", "domain": {{ ...complete domain record... }}}}
   - {{"op": "remove_clock_domain", "name": "<existing>"}}   (rejected while any signal still references it)
   - {{"op": "update_clock_domain", "name": "<existing>", "fields": {{ ... }}}}
   - {{"op": "add_cdc", "cdc": {{ ...complete cdc record... }}}}
   - {{"op": "remove_cdc", "signal": "<signal name of an existing cdc entry>"}}
   - {{"op": "set_reset_sequence", "steps": [ ...complete replacement list... ]}}
   - {{"op": "set_power_sequence", "steps": [ ...complete replacement list... ]}}
   Sequences are whole-list replacements by design - return the ENTIRE new
   ordered list, not a diff. Signal records use exactly the keys shown in the
   current definition. Do NOT emit a patch for a purely informational
   question - just answer it.
"""
