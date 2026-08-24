"""
Saved custom PHY architectures: named {blocks, edges} JSON documents under
<working_dir>/architectures/<name>.json (settings.py roots pattern - current
working dir first, previous ones stay readable).

The BUILT-IN architectures live frontend-side in
frontend/src/phyArchitectures.js (frontend-static by earlier design); this
module only stores user-saved custom ones. The frontend merges the two lists
- see audits/2026-08-21-arch-chat-feature.md for the merge contract.

POST with an existing name overwrites it - that's the ordinary "save again
after more chat edits" flow, not an error.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from arch_chat import normalize_architecture
from settings import all_architectures_roots, architectures_root

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_architecture_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "architecture name must be 1-64 characters of letters, digits, '_' or '-' "
            f"(got {name!r})"
        )
    return name


def save_architecture(
    name: str,
    architecture: Any,
    phy_type: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Validate + persist one named architecture. Returns the stored entry
    (same shape list_architectures() items have). Raises ValueError on a bad
    name or unusable architecture shape."""
    validate_architecture_name(name)
    arch = normalize_architecture(architecture)
    entry = {
        "name": name,
        "label": (label or "").strip() or name,
        "phy_type": phy_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "architecture": arch,
    }
    path = architectures_root() / f"{name}.json"
    if path.exists():
        # Preserve the original creation time across overwrites.
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


def list_architectures() -> list[dict[str, Any]]:
    """All saved architectures across current + previous architectures roots
    (current root wins on a name collision), newest first."""
    seen: dict[str, dict[str, Any]] = {}
    for root in all_architectures_roots():
        for path in sorted(root.glob("*.json")):
            name = path.stem
            if name in seen:
                continue
            try:
                entry = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(entry, dict) or "architecture" not in entry:
                continue
            entry.setdefault("name", name)
            seen[name] = entry
    return sorted(
        seen.values(),
        key=lambda e: e.get("updated_at") or e.get("created_at") or "",
        reverse=True,
    )
