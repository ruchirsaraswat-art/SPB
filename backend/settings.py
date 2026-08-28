"""
Tool settings: where the tool creates its data (cycle 5, split cycle 6).

The WORKING DIRECTORY is the main user-visible setting, under which the tool
creates the sub-trees it needs on demand:

    <working_dir>/libraries/       filed design libraries (lib/cell/view)
    <working_dir>/runs/            Circuit_Builder run directories
    <working_dir>/research_runs/   Circuit_Researcher run directories

Default working dir = this checkout's root (the pre-cycle-5 hardcoded
location), so out of the box nothing changes. Internals are keyed by the
DERIVED sub-paths (libraries_root()/runs_root()/research_root()) so a future
split into independent settings only touches this module.

Cycle 6 adds one such split: LIBRARIES_DIR is an optional, independent
override for where design libraries are filed. Unset (the default) -
libraries stay at <working_dir>/libraries exactly as before, no migration.
Set - libraries_root()/all_libraries_roots() use it instead, while the
working-dir-derived libraries subtree (current AND previous working dirs)
stays on the search path too, so cells filed before the override was turned
on stay listable. Same never-moves-data / previous-locations-stay-readable
model as the working dir: the old override is recorded in
"previous_libraries_dirs" (newest first).

Persistence: a small JSON file at ~/.config/analog-spec-tool/settings.json
(XDG_CONFIG_HOME honored; ANALOG_SPEC_TOOL_SETTINGS env var overrides the
whole path - used by the unit tests). Deliberately OUTSIDE the working dir
itself, so the settings survive pointing the working dir somewhere new.

Every accessor re-reads the file, so a PUT /api/settings takes effect on the
next request with no server restart - the file is tiny and this is a
one-designer local tool, so there is nothing to cache.

Changing the working dir NEVER moves existing data. The old working dir is
recorded in "previous_working_dirs" (newest first) and run/library listing +
schematic resolution search current-then-previous roots, so old runs and
filed cells stay readable from their old location. Moving files is a manual
operation the settings UI tells the user about.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

# The pre-cycle-5 hardcoded location: this checkout's root.
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKING_DIR = BASE_DIR

# Data must never land inside the tool's own source trees - a run dir or
# library dropped into backend/ or frontend/ would get tangled with code,
# dev-server watching, and the Circuit_Builder write-deny rules.
_FORBIDDEN_SUBTREES = (BASE_DIR / "backend", BASE_DIR / "frontend")

_SUBDIRS = {
    "libraries": "libraries",
    "runs": "runs",
    "research_runs": "research_runs",
    # Architecture-discussion chat transcripts (one JSON per session) and
    # user-saved custom PHY architectures (one JSON per name) - see
    # backend/arch_chat.py and the /api/arch_chat + /api/architectures
    # endpoints in main.py.
    "arch_chats": "arch_chats",
    "architectures": "architectures",
    # AFE<->digital interface workspace (2026-08-23 digital-arch spec):
    # saved interface definitions (one JSON per name) and the interface
    # consultant's chat transcripts - see backend/interfaces.py /
    # backend/if_chat.py and the /api/interfaces + /api/if_chat endpoints.
    "interfaces": "interfaces",
    "if_chats": "if_chats",
    # Firmware workspace (2026-08-23 firmware-section spec): firmware chat
    # transcripts, the generated firmware source trees
    # (firmware/<phy_type>/), and the per-fw_generate run bookkeeping
    # (fw_runs/<run_id>/ - spec/status/logs, Token_Optimizer visibility) -
    # see backend/fw_chat.py / backend/fw_generate.py.
    "fw_chats": "fw_chats",
    "firmware": "firmware",
    "fw_runs": "fw_runs",
    # Specification-document intake (2026-08-23 rtl-gen spec, section a):
    # spec_docs/<phy_type>/<doc_id>.<ext> + <doc_id>.meta.json + the per-PHY
    # _status.json answer file that suppresses the intake banner - see
    # backend/spec_docs.py.
    "spec_docs": "spec_docs",
}


def settings_file() -> Path:
    override = os.environ.get("ANALOG_SPEC_TOOL_SETTINGS")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg else Path.home() / ".config"
    return config_home / "analog-spec-tool" / "settings.json"


def _load() -> dict[str, Any]:
    path = settings_file()
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic on POSIX - no torn settings file


def working_dir() -> Path:
    raw = _load().get("working_dir")
    if not raw:
        return DEFAULT_WORKING_DIR
    return Path(str(raw)).expanduser()


def previous_working_dirs() -> list[Path]:
    """Previously-configured working dirs, newest-config first. Old data
    lives here and stays readable; nothing is ever moved automatically."""
    out: list[Path] = []
    for raw in _load().get("previous_working_dirs") or []:
        p = Path(str(raw)).expanduser()
        if p not in out:
            out.append(p)
    return out


def _derived(kind: str, root: Path) -> Path:
    return root / _SUBDIRS[kind]


def _current_root(kind: str) -> Path:
    """Current root for one data kind, created on demand (so a freshly
    configured working dir just works without a separate 'initialize' step)."""
    path = _derived(kind, working_dir())
    path.mkdir(parents=True, exist_ok=True)
    return path


def libraries_dir_override() -> Path | None:
    """The independent libraries_dir override, if the user has set one."""
    raw = _load().get("libraries_dir")
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def previous_libraries_dirs() -> list[Path]:
    """Previously-configured libraries_dir overrides, newest-config first.
    Old filed libraries live here and stay readable - nothing is ever moved
    automatically."""
    out: list[Path] = []
    for raw in _load().get("previous_libraries_dirs") or []:
        p = Path(str(raw)).expanduser()
        if p not in out:
            out.append(p)
    return out


def libraries_root() -> Path:
    override = libraries_dir_override()
    if override is not None:
        override.mkdir(parents=True, exist_ok=True)
        return override
    return _current_root("libraries")


def runs_root() -> Path:
    return _current_root("runs")


def research_root() -> Path:
    return _current_root("research_runs")


def arch_chats_root() -> Path:
    return _current_root("arch_chats")


def architectures_root() -> Path:
    return _current_root("architectures")


def interfaces_root() -> Path:
    return _current_root("interfaces")


def if_chats_root() -> Path:
    return _current_root("if_chats")


def fw_chats_root() -> Path:
    return _current_root("fw_chats")


def firmware_root() -> Path:
    return _current_root("firmware")


def fw_runs_root() -> Path:
    return _current_root("fw_runs")


def spec_docs_root() -> Path:
    return _current_root("spec_docs")


def _all_roots(kind: str) -> list[Path]:
    """Current root first (created on demand), then every previous working
    dir's sub-root that actually exists on disk - newest-config first. This
    is the search order for listing/resolution, so data at old locations
    stays readable after the working dir changes."""
    roots = [_current_root(kind)]
    for prev in previous_working_dirs():
        candidate = _derived(kind, prev)
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def all_libraries_roots() -> list[Path]:
    """Search order for listing/resolving filed libraries. Current effective
    root first (the override if set, else <working_dir>/libraries), then any
    previous libraries_dir overrides, then the working-dir-derived libraries
    subtree for the current AND every previous working dir - so cells filed
    before an override existed (or at an old working dir) stay listable."""
    roots = [libraries_root()]
    for prev in previous_libraries_dirs():
        if prev.is_dir() and prev not in roots:
            roots.append(prev)
    for wd in [working_dir()] + previous_working_dirs():
        candidate = _derived("libraries", wd)
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def all_runs_roots() -> list[Path]:
    return _all_roots("runs")


def all_research_roots() -> list[Path]:
    return _all_roots("research_runs")


def all_architectures_roots() -> list[Path]:
    return _all_roots("architectures")


def all_interfaces_roots() -> list[Path]:
    return _all_roots("interfaces")


def all_spec_docs_roots() -> list[Path]:
    return _all_roots("spec_docs")


def _validate_dir(raw: str, label: str) -> Path:
    """Validate a candidate directory for setting `label`: expand ~, require
    absolute, refuse the tool's own source trees, create it if missing, and
    prove it is actually writable. Returns the resolved Path; raises
    ValueError with a user-facing message otherwise. Shared by
    validate_working_dir and validate_libraries_dir."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"{label} must not be empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"{label} must be an absolute path (or start with ~); got {raw!r}"
        )
    path = Path(os.path.normpath(path))
    for forbidden in _FORBIDDEN_SUBTREES:
        if path == forbidden or path.is_relative_to(forbidden):
            raise ValueError(
                f"{label} must not be inside the tool's source tree ({forbidden}) - "
                "runs/libraries there would get tangled with the tool's own code"
            )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"could not create {label} {path}: {exc}") from None
    if not path.is_dir():
        raise ValueError(f"{label} {path} exists but is not a directory")
    # Prove writability with a real probe file - os.access() can lie on
    # network/readonly mounts, and an unwritable dir would otherwise only
    # surface as a failed run later.
    probe = path / f".spb_write_probe_{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"{label} {path} is not writable: {exc}") from None
    return path


def validate_working_dir(raw: str) -> Path:
    """Validate a candidate working dir. See _validate_dir."""
    return _validate_dir(raw, "working_dir")


def validate_libraries_dir(raw: str) -> Path:
    """Validate a candidate libraries_dir override. See _validate_dir."""
    return _validate_dir(raw, "libraries_dir")


def set_working_dir(raw: str) -> dict[str, Any]:
    """Validate + persist a new working dir. The old one (if different) is
    pushed onto previous_working_dirs so its data stays discoverable. Data is
    never moved. Returns the API payload for the new state."""
    new = validate_working_dir(raw)
    old = working_dir()
    data = _load()
    if new != old:
        prevs = [str(old)] + [
            p for p in (data.get("previous_working_dirs") or []) if p and Path(str(p)).expanduser() != old
        ]
        # The new current dir shouldn't also be listed as "previous".
        prevs = [p for p in prevs if Path(str(p)).expanduser() != new]
        data["previous_working_dirs"] = prevs
    data["working_dir"] = str(new)
    _save(data)
    return settings_payload()


def set_libraries_dir(raw: str | None) -> dict[str, Any]:
    """Validate + persist (or clear) the libraries_dir override. Empty/None
    clears it - libraries then fall back to <working_dir>/libraries. The old
    override (if any and different from the new one) is pushed onto
    previous_libraries_dirs so its filed cells stay discoverable. Data is
    never moved. Returns the API payload for the new state."""
    raw = (raw or "").strip()
    new = validate_libraries_dir(raw) if raw else None
    old = libraries_dir_override()
    data = _load()
    if new != old:
        prevs = [str(old)] if old is not None else []
        prevs += [
            p for p in (data.get("previous_libraries_dirs") or [])
            if p and Path(str(p)).expanduser() not in (old, new)
        ]
        if new is not None:
            prevs = [p for p in prevs if Path(str(p)).expanduser() != new]
        data["previous_libraries_dirs"] = prevs
    if new is None:
        data.pop("libraries_dir", None)
    else:
        data["libraries_dir"] = str(new)
    _save(data)
    return settings_payload()


def settings_payload() -> dict[str, Any]:
    """GET /api/settings response: current + default working dir, derived
    sub-paths, and where any existing (previous-location) data lives."""
    current = working_dir()
    prevs = previous_working_dirs()
    previous_data = []
    for prev in prevs:
        kinds = []
        for k in _SUBDIRS:
            sub = _derived(k, prev)
            try:
                if sub.is_dir() and any(sub.iterdir()):
                    kinds.append(k)
            except OSError:
                continue
        previous_data.append({"working_dir": str(prev), "has_data": kinds})

    lib_override = libraries_dir_override()
    lib_previous_data = []
    for prev in previous_libraries_dirs():
        has_data = False
        try:
            has_data = prev.is_dir() and any(prev.iterdir())
        except OSError:
            has_data = False
        lib_previous_data.append({"libraries_dir": str(prev), "has_data": has_data})

    return {
        "working_dir": str(current),
        "default_working_dir": str(DEFAULT_WORKING_DIR),
        "is_default": current == DEFAULT_WORKING_DIR,
        "derived": {k: str(_derived(k, current)) for k in _SUBDIRS},
        "previous_working_dirs": previous_data,
        "settings_file": str(settings_file()),
        # Independent libraries location override (cycle 6) - unset means
        # libraries live at derived["libraries"] (<working_dir>/libraries).
        "libraries_dir": str(lib_override) if lib_override else None,
        "default_libraries_dir": str(_derived("libraries", current)),
        "libraries_dir_is_default": lib_override is None,
        "libraries_root": str(libraries_root()),
        "previous_libraries_dirs": lib_previous_data,
    }
