"""
Schematic sub-window support (cycle 4).

Resolution: a highlighted diagram block maps to a topology (the frontend
already does block -> topology); this module maps a topology to "the best
schematic we have for it right now":

  1. A cell filed in a design library (libraries/<lib>/<cell>/ - see
     backend/libraries.py) whose provenance records that topology, if it has
     a rendered PNG view. Filed cells are the curated result the user chose
     to keep, so they beat raw run dirs; most recently filed wins.
  2. Otherwise the most recent successful run for that topology whose
     summary recorded a schematic PNG that still exists on disk. Runs from
     before the topology field existed are treated as the built-in
     "nmos_current_mirror" flow (the only thing the tool built back then).
  3. Otherwise a defined empty state ("No schematic yet ...") - the frontend
     shows it verbatim instead of a broken image.

The default view is always the already-rendered PNG served through the
existing file endpoints (works headless, no X needed). "Open in xschem" is
the secondary, display-gated action: interactive xschem spawned detached
with cwd = a directory holding an xschemrc that resolves sky130 symbols
(run dirs already have one; library cell dirs get one written on first
launch). Never `xschem -x` here - that flag is for headless PNG export and
would defeat the point of an interactive window.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from circuit_builder import _write_project_xschemrc
from settings import all_libraries_roots, all_runs_roots

# Fallback X11 display for launching interactive xschem: this machine's
# desktop session runs on :2 (main.py's open-schematic endpoint uses the
# same fallback). Only used if the backend's own DISPLAY is unset/dead, and
# only if the display's local socket actually exists.
FALLBACK_DISPLAY = ":2"
X11_SOCKET_DIR = Path("/tmp/.X11-unix")

EMPTY_STATE_MESSAGE = "No schematic yet — submit a spec for this block first."

# Legacy runs (before the topology field existed) were all the built-in
# current-mirror flow; treat a missing spec.topology as that.
_LEGACY_DEFAULT_TOPOLOGY = "nmos_current_mirror"

_TOPOLOGY_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def validate_topology_name(topology: str) -> None:
    """Path-safety gate for topology values arriving via URL: they become
    directory-scan comparisons only, but reject junk early and loudly."""
    if not _TOPOLOGY_NAME_RE.match(topology or ""):
        raise ValueError(f"invalid topology name {topology!r}")


# --- display availability ---------------------------------------------------


def _local_x_socket_ok(display: str, x11_dir: Path) -> bool:
    """True if `display` is usable: a local ":N"/"":N.M" display must have its
    /tmp/.X11-unix/XN socket present; anything else (tcp host:N) can't be
    checked cheaply, so trust the env var."""
    m = re.match(r"^:(\d+)(\.\d+)?$", display)
    if not m:
        return True
    return (x11_dir / f"X{m.group(1)}").exists()


def display_status(
    environ: Optional[Mapping[str, str]] = None,
    x11_dir: Path = X11_SOCKET_DIR,
) -> dict[str, Any]:
    """Whether an interactive X display is reachable right now, and which one.
    Checked at request time (not cached): the user can log out of the desktop
    session while the backend keeps running. Wayland-only sessions without
    XWayland count as unavailable - xschem is a Tcl/Tk X11 app."""
    env = os.environ if environ is None else environ
    for display in (env.get("DISPLAY"), FALLBACK_DISPLAY):
        if display and _local_x_socket_ok(display, x11_dir):
            return {"available": True, "display": display}
    return {
        "available": False,
        "display": None,
        "reason": "No X display reachable from the backend (DISPLAY unset or its socket is "
        "gone, and the fallback desktop display isn't up). xschem needs X11/XWayland - "
        "log into the machine's desktop session to enable this.",
    }


# --- resolution -------------------------------------------------------------


def _pick_view(cell_dir: Path, cell: str, suffix: str, exclude_tb: bool = False) -> Optional[str]:
    """Prefer the canonical <cell><suffix> view; else the first other file
    with that suffix (excluding testbench schematics when asked)."""
    canonical = cell_dir / f"{cell}{suffix}"
    if canonical.is_file():
        return canonical.name
    for f in sorted(cell_dir.glob(f"*{suffix}")):
        if exclude_tb and f.name.endswith(f"_tb{suffix}"):
            continue
        if f.is_file():
            return f.name
    return None


def _library_candidate(topology: str, libs_dirs: list[Path]) -> Optional[dict[str, Any]]:
    """Best filed cell for a topology across all libraries roots (current
    working dir first, then previous ones - see settings.py). Latest
    filed_at wins; on a tie the earlier (newer-config) root wins because
    replacement requires strictly-greater filed_at."""
    best: Optional[dict[str, Any]] = None
    for libs_dir in libs_dirs:
        if not libs_dir.is_dir():
            continue
        for prov_path in libs_dir.glob("*/*/provenance.json"):
            try:
                prov = json.loads(prov_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if prov.get("topology") != topology:
                continue
            cell_dir = prov_path.parent
            cell = cell_dir.name
            library = cell_dir.parent.name
            png = _pick_view(cell_dir, cell, ".png")
            if png is None:
                continue  # no rendered view - a run dir may still have one
            candidate = {
                "found": True,
                "source": "library",
                "library": library,
                "cell": cell,
                "dir": str(cell_dir),
                "png": png,
                "sch": _pick_view(cell_dir, cell, ".sch", exclude_tb=True),
                "run_id": prov.get("run_id"),
                "label": prov.get("label"),
                "chosen_architecture": prov.get("chosen_architecture"),
                "filed_at": prov.get("filed_at") or "",
                "png_url": f"/api/libraries/{library}/{cell}/file/{png}",
            }
            if best is None or candidate["filed_at"] > best["filed_at"]:
                best = candidate
    return best


def _find_run_dir(run_id: str, runs_dirs: list[Path]) -> Optional[Path]:
    """Where a run's directory actually lives: current runs root first, then
    previous working dirs' roots (runs are never moved when the working dir
    changes, so old runs resolve at their old location)."""
    for root in runs_dirs:
        candidate = root / run_id
        if candidate.is_dir():
            return candidate
    return None


def _run_candidate(
    topology: str, runs: Iterable[dict[str, Any]], runs_dirs: list[Path]
) -> Optional[dict[str, Any]]:
    best: Optional[dict[str, Any]] = None
    for r in runs:
        if r.get("state") != "done" or r.get("status") != "success":
            continue
        spec = r.get("spec") or {}
        if (spec.get("topology") or _LEGACY_DEFAULT_TOPOLOGY) != topology:
            continue
        summary = r.get("summary") or {}
        png = summary.get("schematic_png")
        run_id = r.get("run_id")
        if not png or not run_id:
            continue
        run_dir = _find_run_dir(run_id, runs_dirs)
        if run_dir is None or not (run_dir / png).is_file():
            continue
        candidate = {
            "found": True,
            "source": "run",
            "run_id": run_id,
            "dir": str(run_dir),
            "png": png,
            "sch": summary.get("schematic_file"),
            "label": r.get("label"),
            "chosen_architecture": spec.get("chosen_architecture"),
            "created_at": r.get("created_at") or "",
            "png_url": f"/api/runs/{run_id}/file/{png}",
        }
        if best is None or candidate["created_at"] > best["created_at"]:
            best = candidate
    return best


def resolve_schematic(
    topology: str,
    runs: Iterable[dict[str, Any]],
    libs_dir: Path | list[Path] | None = None,
    runs_dir: Path | list[Path] | None = None,
) -> dict[str, Any]:
    """Best available schematic for a topology: filed library cell first,
    then latest successful run with a PNG, else the defined empty state.

    libs_dir/runs_dir default to the settings-derived search roots (current
    working dir's tree first, then previous working dirs' - resolved at CALL
    time so a settings change needs no restart); a single Path is accepted
    for tests/explicit callers."""
    validate_topology_name(topology)
    libs_dirs = (
        all_libraries_roots() if libs_dir is None
        else ([libs_dir] if isinstance(libs_dir, Path) else list(libs_dir))
    )
    runs_dirs = (
        all_runs_roots() if runs_dir is None
        else ([runs_dir] if isinstance(runs_dir, Path) else list(runs_dir))
    )
    candidate = _library_candidate(topology, libs_dirs) or _run_candidate(
        topology, runs, runs_dirs
    )
    if candidate is None:
        return {"topology": topology, "found": False, "message": EMPTY_STATE_MESSAGE}
    return {"topology": topology, **candidate}


# --- interactive xschem launch ---------------------------------------------

# One live xschem per schematic file: repeated clicks on the same block
# should not spawn unbounded GUI windows. Keyed by resolved .sch path; a
# process that has exited frees its slot (poll() picks that up).
_open_procs: dict[str, subprocess.Popen] = {}
_open_lock = threading.Lock()


class DisplayUnavailable(RuntimeError):
    pass


class LaunchFailed(RuntimeError):
    pass


def launch_xschem(sch_path: Path, cwd: Path) -> dict[str, Any]:
    """Spawn interactive xschem on `sch_path`, detached, with cwd = a
    directory whose xschemrc resolves sky130 symbols (written on demand for
    library cell dirs). Raises DisplayUnavailable when no X display is
    reachable, LaunchFailed when xschem dies immediately."""
    status = display_status()
    if not status["available"]:
        raise DisplayUnavailable(status["reason"])
    if not sch_path.is_file():
        raise LaunchFailed(f"schematic file {sch_path} does not exist")

    key = str(sch_path.resolve())
    with _open_lock:
        existing = _open_procs.get(key)
        if existing is not None and existing.poll() is None:
            return {
                "status": "already_open",
                "pid": existing.pid,
                "display": status["display"],
                "detail": "an xschem window for this schematic is already open - reusing it",
            }

        # Library cell dirs are created by filing (no xschemrc); run dirs
        # always have one already. Never clobber an existing file.
        if not (cwd / "xschemrc").is_file():
            _write_project_xschemrc(cwd)

        env = os.environ.copy()
        env["DISPLAY"] = status["display"]
        log_path = cwd / "xschem_gui.log"
        try:
            with open(log_path, "w") as logf:
                proc = subprocess.Popen(
                    ["xschem", str(sch_path)],
                    cwd=str(cwd),
                    env=env,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # detach: backend restart/exit must not reap the GUI
                )
        except FileNotFoundError as exc:
            raise LaunchFailed(f"could not launch xschem: {exc}") from None
        _open_procs[key] = proc

    # Fast-failure window: a doomed launch (bad file, dead display) usually
    # exits within a second or two; still-running means a window is opening.
    try:
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return {"status": "launched", "pid": proc.pid, "display": status["display"]}
    tail = log_path.read_text()[-800:] if log_path.exists() else ""
    with _open_lock:
        if _open_procs.get(key) is proc:
            del _open_procs[key]
    raise LaunchFailed(
        f"xschem exited immediately (code {proc.returncode}) using DISPLAY={status['display']}: {tail}"
    )


def sch_and_cwd_for_resolved(resolved: dict[str, Any]) -> tuple[Path, Path]:
    """Map a resolve_schematic() result onto a concrete .sch path + cwd,
    without launching anything - shared by launch_for_resolved (local X11
    "Open in xschem") and xschem_session.py (browser-reachable VNC session).
    The cwd comes from the candidate's own recorded directory ("dir", set
    server-side during resolution - which may be under a PREVIOUS working dir's
    tree for old data; callers must never feed this a client-supplied dict).
    ValueError when the resolved result has no .sch view (PNG-only cell) or
    wasn't found at all - callers turn that into a 404/409, not a crash."""
    if not resolved.get("found"):
        raise ValueError(EMPTY_STATE_MESSAGE)
    sch = resolved.get("sch")
    if not sch:
        raise ValueError(
            "this block's filed result has a rendered PNG but no .sch schematic view, "
            "so there is nothing for xschem to open"
        )
    cwd = Path(resolved["dir"])
    return cwd / sch, cwd


def launch_for_resolved(resolved: dict[str, Any]) -> dict[str, Any]:
    """Map a resolve_schematic() result onto a concrete .sch path + cwd and
    launch xschem there (detached, on the backend's local X display - see
    launch_xschem). ValueError propagates from sch_and_cwd_for_resolved."""
    sch_path, cwd = sch_and_cwd_for_resolved(resolved)
    return launch_xschem(sch_path, cwd)
