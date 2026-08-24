"""
AFE<->digital interface workspace: data model, validation, chat patch ops and
storage for the interface definitions edited by the middle workspace panel
(feature spec: audits/2026-08-23-digital-arch-spec.md, section c).

An interface definition is the single source of truth for the contract
between the AFE diagram and the digital diagram:

    {name, phy_type, clock_domains, signals, cdc_points,
     reset_sequence, power_sequence}

Validation splits into HARD ERRORS (block a save with 422; duplicate/invalid
names, unknown enum values, unknown clock_domain, dangling cdc references,
width < 1) and SOFT WARNINGS (returned alongside + stored with the entry,
shown inline in the editor, never blocking - line-rate boundary crossings,
async signals with no CDC plan, >200 MHz word clocks for sky130). Dangling
afe_block/digital_block ids are a soft warning too, and only checkable when
the two architectures are supplied (the if_chat request carries them; a bare
POST /api/interfaces does not - blocks get renamed/removed independently, so
this must never hard-fail a save).

NOTE on the `direction` enum: the spec's prose enumerates "a2d"/"d2a", but
its own worked SerDes example (which must round-trip with zero hard errors)
marks clk_ref and rstn_por as "ext" - board/SoC-sourced signals that belong
to neither side. "ext" is therefore part of the vocabulary.

Deferred (documented, not built): the "d2a quasi_static bus not mentioned in
any FSM/cal block's adapt_targets" warning needs per-block FIELD VALUES from
the digital diagram, which the diagram data shape doesn't carry today -
validate_interface accepts an adapt_targets_text hook for when it does.

Storage mirrors architectures.py exactly: <working_dir>/interfaces/<name>.json,
current-then-previous roots, overwrite-by-name is the ordinary re-save flow.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from settings import all_interfaces_roots, interfaces_root

# ---------------------------------------------------------------------------
# Enums (validated server-side, rendered as dropdowns in the table editor)

DIRECTIONS = ("a2d", "d2a", "ext")
RATES = ("line", "word", "quasi_static", "async_event")
SIGNAL_GROUPS = ("data", "clock", "control", "status", "reset", "power")
HANDSHAKES = ("none", "valid", "valid_ready", "req_ack", "level", "pulse", "2ff_sync")
ASYNC_DOMAIN = "async"

_SIGNAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SKY130_WORD_CLOCK_WARN_MHZ = 200
_SKY130_LOGIC_WARN = "Above ~200 MHz is aggressive for sky130 synthesized logic."

VALID_IF_OPS = {
    "add_signal",
    "remove_signal",
    "update_signal",
    "add_clock_domain",
    "remove_clock_domain",
    "update_clock_domain",
    "add_cdc",
    "remove_cdc",
    "set_reset_sequence",
    "set_power_sequence",
}

_SIGNAL_KEYS = (
    "name", "direction", "width", "clock_domain", "rate", "rate_mhz",
    "group", "handshake", "afe_block", "digital_block", "meaning",
)
_DOMAIN_KEYS = ("name", "freq_mhz", "owner", "source_block", "description")
_CDC_KEYS = ("signal", "from_domain", "to_domain", "strategy", "notes")


def validate_interface_name(name: str) -> str:
    """File-name rules, identical to saved architectures."""
    if not isinstance(name, str) or not _FILE_NAME_RE.match(name):
        raise ValueError(
            "interface name must be 1-64 characters of letters, digits, '_' or '-' "
            f"(got {name!r})"
        )
    return name


# ---------------------------------------------------------------------------
# Normalization + validation


def normalize_interface(data: Any) -> dict[str, Any]:
    """Lenient shape normalization (lists default empty, records copied).
    Raises ValueError only when the overall shape is unusable; per-record
    problems are validate_interface()'s job so they come back as a complete
    error list, not one-at-a-time."""
    if not isinstance(data, dict):
        raise ValueError("interface must be a JSON object")
    out: dict[str, Any] = {
        "name": data.get("name") or "",
        "phy_type": data.get("phy_type"),
    }
    for key in ("clock_domains", "signals", "cdc_points", "reset_sequence", "power_sequence"):
        value = data.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"interface.{key} must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(f"every entry of interface.{key} must be an object (got {item!r})")
        out[key] = [dict(item) for item in value]
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def validate_interface(
    iface: dict[str, Any],
    afe_architecture: dict[str, Any] | None = None,
    digital_architecture: dict[str, Any] | None = None,
    adapt_targets_text: str | None = None,
) -> tuple[list[str], list[str]]:
    """Full validation of a normalized interface. Returns (hard_errors,
    soft_warnings) - hard errors block a save/patch (422); warnings are
    returned + stored + shown inline, never blocking. The optional
    architecture args enable the dangling afe_block/digital_block warning;
    adapt_targets_text is the (currently dormant) hook for the quasi-static-
    bus-vs-adapt-targets cross-check - see module docstring."""
    errors: list[str] = []
    warnings: list[str] = []

    # --- clock domains ---
    domain_names: list[str] = []
    for i, dom in enumerate(iface["clock_domains"]):
        name = dom.get("name")
        where = f"clock_domains[{i}]"
        if not isinstance(name, str) or not _SIGNAL_NAME_RE.match(name):
            errors.append(f"{where}: invalid domain name {name!r} (must match [a-z][a-z0-9_]*)")
            continue
        if name == ASYNC_DOMAIN:
            errors.append(f"{where}: 'async' is reserved (it means 'no clock domain')")
            continue
        if name in domain_names:
            errors.append(f"{where}: duplicate clock domain name {name!r}")
            continue
        domain_names.append(name)
    known_domains = set(domain_names)

    # --- signals ---
    signal_names: list[str] = []
    for i, sig in enumerate(iface["signals"]):
        name = sig.get("name")
        where = f"signals[{i}]" + (f" ({name!r})" if isinstance(name, str) else "")
        if not isinstance(name, str) or not _SIGNAL_NAME_RE.match(name):
            errors.append(f"signals[{i}]: invalid signal name {name!r} (must match [a-z][a-z0-9_]*)")
            name = None
        elif name in signal_names:
            errors.append(f"{where}: duplicate signal name {name!r}")
            name = None
        else:
            signal_names.append(name)

        direction = sig.get("direction")
        if direction not in DIRECTIONS:
            errors.append(
                f"{where}: unknown direction {direction!r} - must be one of: {', '.join(DIRECTIONS)}"
            )
        rate = sig.get("rate")
        if rate not in RATES:
            errors.append(f"{where}: unknown rate {rate!r} - must be one of: {', '.join(RATES)}")
        group = sig.get("group")
        if group not in SIGNAL_GROUPS:
            errors.append(
                f"{where}: unknown group {group!r} - must be one of: {', '.join(SIGNAL_GROUPS)}"
            )
        handshake = sig.get("handshake")
        if handshake not in HANDSHAKES:
            errors.append(
                f"{where}: unknown handshake {handshake!r} - must be one of: {', '.join(HANDSHAKES)}"
            )
        width = sig.get("width")
        if not _is_int(width) or width < 1:
            errors.append(f"{where}: width must be an integer >= 1 (got {width!r})")
        domain = sig.get("clock_domain")
        if domain != ASYNC_DOMAIN and domain not in known_domains:
            errors.append(
                f"{where}: unknown clock_domain {domain!r} - must be 'async' or one of: "
                + (", ".join(sorted(known_domains)) or "(no domains defined)")
            )
        rate_mhz = sig.get("rate_mhz")
        if rate == "word":
            num = _as_number(rate_mhz)
            if num is None:
                errors.append(f"{where}: rate_mhz is required (a number) when rate is 'word'")
            elif num > SKY130_WORD_CLOCK_WARN_MHZ:
                warnings.append(f"{where}: rate_mhz = {rate_mhz} MHz: {_SKY130_LOGIC_WARN}")
        elif rate_mhz is not None and _as_number(rate_mhz) is None:
            errors.append(f"{where}: rate_mhz must be a number when present (got {rate_mhz!r})")

    # --- cdc points ---
    known_signals = set(signal_names)
    cdc_signals: set[str] = set()
    for i, cdc in enumerate(iface["cdc_points"]):
        sig_name = cdc.get("signal")
        where = f"cdc_points[{i}]"
        if sig_name not in known_signals:
            errors.append(f"{where}: references nonexistent signal {sig_name!r}")
        else:
            cdc_signals.add(sig_name)
        for key in ("from_domain", "to_domain"):
            dom = cdc.get(key)
            if dom != ASYNC_DOMAIN and dom not in known_domains:
                errors.append(f"{where}: {key} {dom!r} is not 'async' or a defined clock domain")

    # --- sequences (structural only) ---
    for key in ("reset_sequence", "power_sequence"):
        for i, step in enumerate(iface[key]):
            if not isinstance(step.get("action"), str) or not step.get("action").strip():
                errors.append(f"{key}[{i}]: every step needs a non-empty 'action' string")

    # --- soft warnings on the signal set ---
    for sig in iface["signals"]:
        name = sig.get("name")
        if not isinstance(name, str) or name not in known_signals:
            continue  # already hard-errored above
        label = f"signal '{name}'"
        if sig.get("rate") == "line":
            warnings.append(
                f"{label} is line-rate: per-UI signals should not normally cross the "
                "AFE-digital boundary - the whole point of the deserializer is that "
                "only word-rate signals cross. Double-check this is intentional."
            )
        if (
            sig.get("clock_domain") == ASYNC_DOMAIN
            and name not in cdc_signals
            and sig.get("handshake") != "2ff_sync"
        ):
            warnings.append(
                f"{label} is async with no cdc_points entry and handshake is not "
                "'2ff_sync' - every asynchronous crossing needs a documented "
                "synchronization plan."
            )
        if sig.get("handshake") == "pulse" and name not in cdc_signals:
            warnings.append(
                f"{label} is a pulse with no cdc_points entry - a single-cycle event "
                "must be stretched/synchronized if it crosses domains."
            )

    # --- dangling diagram cross-links (soft; only when the diagrams are known) ---
    if afe_architecture is not None or digital_architecture is not None:
        afe_ids = {
            b.get("id") for b in (afe_architecture or {}).get("blocks", []) if isinstance(b, dict)
        }
        dig_ids = {
            b.get("id")
            for b in (digital_architecture or {}).get("blocks", [])
            if isinstance(b, dict)
        }
        for sig in iface["signals"]:
            name = sig.get("name")
            if afe_architecture is not None and sig.get("afe_block") and sig["afe_block"] not in afe_ids:
                warnings.append(
                    f"signal '{name}': afe_block {sig['afe_block']!r} is not a block id in "
                    "the AFE diagram (renamed/removed?) - dangling link, not an error"
                )
            if (
                digital_architecture is not None
                and sig.get("digital_block")
                and sig["digital_block"] not in dig_ids
            ):
                warnings.append(
                    f"signal '{name}': digital_block {sig['digital_block']!r} is not a block "
                    "id in the digital diagram (renamed/removed?) - dangling link, not an error"
                )

    # --- quasi-static bus vs adapt_targets (dormant hook; see module docstring) ---
    if adapt_targets_text:
        text = adapt_targets_text.lower()
        for sig in iface["signals"]:
            name = sig.get("name")
            if (
                isinstance(name, str)
                and sig.get("direction") == "d2a"
                and sig.get("rate") == "quasi_static"
                and sig.get("group") == "control"
                and name.lower() not in text
            ):
                warnings.append(
                    f"signal '{name}' is a d2a quasi-static control but is not mentioned in "
                    "any FSM/cal block's adaptation/trim target list - who drives it?"
                )

    return errors, warnings


# ---------------------------------------------------------------------------
# Chat patch ops (POST /api/if_chat). Ordered, applied in order against a
# working copy so an add_signal name may be referenced by a later add_cdc in
# the same patch. Mirrors arch_chat.validate_patch's annotate-don't-raise
# contract: invalid ops get an "error" field, valid ops are applied so later
# ops are judged against the best-effort state.


def _signal_record(raw: Any) -> dict[str, Any]:
    rec = {k: raw.get(k) for k in _SIGNAL_KEYS} if isinstance(raw, dict) else {}
    return rec


def _validate_one_signal(sig: dict[str, Any], iface: dict[str, Any]) -> str | None:
    """Validate a single (complete) signal record against the current working
    state by running the full validator on a copy with it appended - keeps
    one source of truth for the per-record rules."""
    probe = dict(iface)
    probe["signals"] = iface["signals"] + [sig]
    errors, _ = validate_interface(probe)
    idx = len(iface["signals"])
    # Strip the positional "signals[i] (...):" prefix - in a patch-op error it
    # would point at a working-copy index the user never sees.
    mine = [
        e.split(": ", 1)[1] if ": " in e else e
        for e in errors
        if e.startswith(f"signals[{idx}]")
    ]
    return "; ".join(mine) or None


def apply_if_patch(patch: Any, interface: dict[str, Any]) -> tuple[list[dict], bool, dict[str, Any]]:
    """Validate + apply {"ops": [...]} against a normalized interface. Returns
    (annotated_ops, all_valid, resulting_interface). Semantics the CLIENT
    APPLIER MUST MIRROR (frontend/src/interfaceStore.js):
      - remove_signal implicitly removes its cdc_points entries;
      - update_signal renaming a signal (fields.name) renames its cdc_points
        references too;
      - update_clock_domain renaming a domain renames signal.clock_domain and
        cdc from/to references;
      - remove_clock_domain is rejected while any signal or cdc entry still
        references the domain;
      - set_reset_sequence/set_power_sequence are whole-list replacements.
    Never raises for bad op content."""
    ops_in = patch.get("ops") if isinstance(patch, dict) else None
    if not isinstance(ops_in, list):
        return (
            [{"op": "invalid", "error": 'patch must be an object {"ops": [...]}'}],
            False,
            interface,
        )

    work = json.loads(json.dumps(interface))  # deep copy
    annotated: list[dict] = []
    all_valid = True

    def signal_names() -> list[str]:
        return [s.get("name") for s in work["signals"]]

    def domain_names() -> list[str]:
        return [d.get("name") for d in work["clock_domains"]]

    for raw in ops_in:
        op = dict(raw) if isinstance(raw, dict) else {"op": repr(raw)}
        error: str | None = None
        kind = op.get("op")

        if kind not in VALID_IF_OPS:
            error = f"unknown op {kind!r} - must be one of: {', '.join(sorted(VALID_IF_OPS))}"

        elif kind == "add_signal":
            sig = _signal_record(op.get("signal"))
            error = _validate_one_signal(sig, work)
            if error is None:
                work["signals"].append(sig)

        elif kind == "remove_signal":
            name = op.get("name")
            if name not in signal_names():
                error = f"remove_signal: no signal named {name!r}"
            else:
                work["signals"] = [s for s in work["signals"] if s.get("name") != name]
                # cascade: a removed signal takes its cdc entries with it
                work["cdc_points"] = [c for c in work["cdc_points"] if c.get("signal") != name]

        elif kind == "update_signal":
            name = op.get("name")
            fields = op.get("fields")
            if name not in signal_names():
                error = f"update_signal: no signal named {name!r}"
            elif not isinstance(fields, dict) or not fields:
                error = "update_signal requires a non-empty 'fields' object"
            else:
                idx = signal_names().index(name)
                merged = {**work["signals"][idx], **{k: v for k, v in fields.items() if k in _SIGNAL_KEYS}}
                others = dict(work)
                others["signals"] = [s for j, s in enumerate(work["signals"]) if j != idx]
                error = _validate_one_signal(merged, others)
                if error is None:
                    work["signals"][idx] = merged
                    new_name = merged.get("name")
                    if new_name != name:
                        # cascade rename into cdc references
                        for c in work["cdc_points"]:
                            if c.get("signal") == name:
                                c["signal"] = new_name

        elif kind == "add_clock_domain":
            dom = op.get("domain")
            dom = {k: dom.get(k) for k in _DOMAIN_KEYS} if isinstance(dom, dict) else {}
            dname = dom.get("name")
            if not isinstance(dname, str) or not _SIGNAL_NAME_RE.match(dname) or dname == ASYNC_DOMAIN:
                error = f"add_clock_domain: invalid domain name {dname!r}"
            elif dname in domain_names():
                error = f"add_clock_domain: domain {dname!r} already exists"
            else:
                work["clock_domains"].append(dom)

        elif kind == "remove_clock_domain":
            name = op.get("name")
            if name not in domain_names():
                error = f"remove_clock_domain: no domain named {name!r}"
            else:
                users = [s.get("name") for s in work["signals"] if s.get("clock_domain") == name]
                cdc_users = [
                    c.get("signal")
                    for c in work["cdc_points"]
                    if name in (c.get("from_domain"), c.get("to_domain"))
                ]
                if users or cdc_users:
                    refs = ", ".join([str(u) for u in users + cdc_users])
                    error = (
                        f"remove_clock_domain: domain {name!r} is still referenced by: {refs} "
                        "- move those signals/cdc entries first"
                    )
                else:
                    work["clock_domains"] = [
                        d for d in work["clock_domains"] if d.get("name") != name
                    ]

        elif kind == "update_clock_domain":
            name = op.get("name")
            fields = op.get("fields")
            if name not in domain_names():
                error = f"update_clock_domain: no domain named {name!r}"
            elif not isinstance(fields, dict) or not fields:
                error = "update_clock_domain requires a non-empty 'fields' object"
            else:
                new_name = fields.get("name", name)
                if not isinstance(new_name, str) or not _SIGNAL_NAME_RE.match(new_name) or new_name == ASYNC_DOMAIN:
                    error = f"update_clock_domain: invalid new domain name {new_name!r}"
                elif new_name != name and new_name in domain_names():
                    error = f"update_clock_domain: domain {new_name!r} already exists"
                else:
                    idx = domain_names().index(name)
                    merged = {**work["clock_domains"][idx], **{k: v for k, v in fields.items() if k in _DOMAIN_KEYS}}
                    work["clock_domains"][idx] = merged
                    if new_name != name:
                        # cascade rename into signals + cdc references
                        for s in work["signals"]:
                            if s.get("clock_domain") == name:
                                s["clock_domain"] = new_name
                        for c in work["cdc_points"]:
                            for key in ("from_domain", "to_domain"):
                                if c.get(key) == name:
                                    c[key] = new_name

        elif kind == "add_cdc":
            cdc = op.get("cdc")
            cdc = {k: cdc.get(k) for k in _CDC_KEYS} if isinstance(cdc, dict) else {}
            problems = []
            if cdc.get("signal") not in signal_names():
                problems.append(f"references nonexistent signal {cdc.get('signal')!r}")
            for key in ("from_domain", "to_domain"):
                dom = cdc.get(key)
                if dom != ASYNC_DOMAIN and dom not in domain_names():
                    problems.append(f"{key} {dom!r} is not 'async' or a defined clock domain")
            if problems:
                error = "add_cdc: " + "; ".join(problems)
            else:
                work["cdc_points"].append(cdc)

        elif kind == "remove_cdc":
            sig_name = op.get("signal")
            if not any(c.get("signal") == sig_name for c in work["cdc_points"]):
                error = f"remove_cdc: no cdc entry for signal {sig_name!r}"
            else:
                work["cdc_points"] = [
                    c for c in work["cdc_points"] if c.get("signal") != sig_name
                ]

        elif kind in ("set_reset_sequence", "set_power_sequence"):
            steps = op.get("steps")
            if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
                error = f"{kind} requires 'steps' as a list of step objects"
            elif any(not isinstance(s.get("action"), str) or not s["action"].strip() for s in steps):
                error = f"{kind}: every step needs a non-empty 'action' string"
            else:
                key = "reset_sequence" if kind == "set_reset_sequence" else "power_sequence"
                work[key] = [dict(s) for s in steps]

        if error:
            op["error"] = error
            all_valid = False
        annotated.append(op)

    return annotated, all_valid, work


# ---------------------------------------------------------------------------
# Storage (mirrors architectures.py: one JSON per name under interfaces/)


def save_interface(
    name: str,
    interface: Any,
    phy_type: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Validate + persist one named interface definition. Hard errors raise
    ValueError (-> 422); soft warnings are stored with (and returned on) the
    entry, like the runs' _active_soft_warnings. Overwrite-by-name preserves
    created_at, exactly like save_architecture."""
    validate_interface_name(name)
    iface = normalize_interface(interface)
    errors, warnings = validate_interface(iface)
    if errors:
        raise ValueError(
            "interface definition failed validation:\n" + "\n".join(f"- {e}" for e in errors)
        )
    entry = {
        "name": name,
        "label": (label or "").strip() or name,
        "phy_type": phy_type or iface.get("phy_type"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "warnings": warnings,
        "interface": iface,
    }
    path = interfaces_root() / f"{name}.json"
    if path.exists():
        try:
            old = json.loads(path.read_text())
            entry["created_at"] = old.get("created_at", entry["created_at"])
        except (json.JSONDecodeError, OSError):
            pass
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2))
    tmp.replace(path)
    return entry


def list_interfaces() -> list[dict[str, Any]]:
    """All saved interface definitions across current + previous roots
    (current root wins on a name collision), newest first."""
    seen: dict[str, dict[str, Any]] = {}
    for root in all_interfaces_roots():
        for path in sorted(root.glob("*.json")):
            name = path.stem
            if name in seen:
                continue
            try:
                entry = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(entry, dict) or "interface" not in entry:
                continue
            entry.setdefault("name", name)
            seen[name] = entry
    return sorted(
        seen.values(),
        key=lambda e: e.get("updated_at") or e.get("created_at") or "",
        reverse=True,
    )
