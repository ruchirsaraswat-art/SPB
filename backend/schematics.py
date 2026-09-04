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
from datetime import datetime, timezone
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


# --- draw-a-new-schematic-by-hand (a block with NO resolved schematic yet) --
#
# Every gap block (DDR's Vref Gen, ODT, ...) hits resolve_schematic's empty
# state: no filed cell, no successful run. This section is the other side of
# that: create a blank-but-valid .sch at libraries/<lib>/<cell>/<cell>.sch (the
# SAME library/cell/view convention libraries.py already owns - never a new
# location), open a live xschem session on it directly (bypassing
# resolve_schematic entirely, since there is nothing to resolve to yet - no
# PNG exists), and later "capture" what the user drew: headlessly render a PNG
# and extract a netlist so resolve_schematic finds it from then on exactly
# like a Circuit_Builder run's output.
#
# Blank-schematic format validated against libraries/cal_runs/current_mirror/
# current_mirror.sch (the one hand-authored cell already in this tree) and
# empirically confirmed to open/render/netlist cleanly (2026-09): it is
# exactly the six-line header xschem itself writes for a brand-new schematic
# (v/G/K/V/S/E; no N/C/T lines - those only appear once something is placed).
_BLANK_SCH_TEMPLATE = (
    "v {xschem version=3.4.4 file_version=1.2}\n"
    "G {}\n"
    "K {}\n"
    "V {}\n"
    "S {}\n"
    "E {}\n"
)


class CellExists(RuntimeError):
    """Refused: `library`/`cell` already has a .sch - the caller must offer
    "open it" instead of silently clobbering someone's existing work."""

    def __init__(self, library: str, cell: str):
        self.library = library
        self.cell = cell
        super().__init__(
            f"{library}/{cell} already has a schematic - open the existing one instead "
            "of creating a new cell with the same name"
        )


class CaptureFailed(RuntimeError):
    pass


def create_blank_cell(
    library: str, cell: str, topology: str, label: Optional[str] = None
) -> dict[str, Any]:
    """Create libraries/<library>/<cell>/<cell>.sch as a blank-but-valid
    xschem schematic, and (re)write that cell dir's xschemrc so sky130
    symbols and every filed library are reachable from its browser the moment
    a live session opens it. Never overwrites an existing .sch (raises
    CellExists); a cell dir that already exists with OTHER views (e.g. a
    prior symbol/model-only run left no .sch) is extended in place, not
    blocked. Raises ValueError on a bad library/cell/topology name (same
    validators the rest of the tool's library picker uses)."""
    from libraries import create_library, find_library_dir, validate_library_name

    validate_library_name(library)
    validate_library_name(cell)
    validate_topology_name(topology)

    lib_dir = find_library_dir(library)
    if lib_dir is None:
        create_library(library)
        lib_dir = find_library_dir(library)
    cell_dir = lib_dir / cell
    sch_path = cell_dir / f"{cell}.sch"
    if sch_path.is_file():
        raise CellExists(library, cell)

    cell_dir.mkdir(parents=True, exist_ok=True)
    sch_path.write_text(_BLANK_SCH_TEMPLATE)

    prov_path = cell_dir / "provenance.json"
    prov: dict[str, Any] = {}
    if prov_path.exists():
        try:
            prov = json.loads(prov_path.read_text())
        except (json.JSONDecodeError, OSError):
            prov = {}
    prov["topology"] = topology
    if label:
        prov["label"] = label
    prov["source"] = "manual"  # hand-drawn in xschem, not a Circuit_Builder run
    prov.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    views = prov.get("views") or []
    if sch_path.name not in views:
        views.append(sch_path.name)
    prov["views"] = views
    prov_path.write_text(json.dumps(prov, indent=2))

    # Written now (not lazily on first session start, unlike the resolved-
    # schematic path) - the whole point of this action is to open a session
    # on this exact file right away.
    _write_project_xschemrc(cell_dir)

    return {
        "library": library, "cell": cell, "dir": str(cell_dir),
        "sch": sch_path.name, "topology": topology,
    }


def sch_and_cwd_for_cell(library: str, cell: str) -> tuple[Path, Path]:
    """A specific library cell's .sch path + cwd, without going through
    resolve_schematic (which requires a rendered PNG to consider a cell
    "found" - exactly the state a freshly-created blank cell, or one whose
    live session hasn't been captured yet, is never in). ValueError when the
    library/cell doesn't exist or has no .sch - callers turn that into a
    404/422, not a crash."""
    from libraries import find_library_dir, validate_library_name

    validate_library_name(library)
    validate_library_name(cell)
    lib_dir = find_library_dir(library)
    cell_dir = (lib_dir / cell) if lib_dir else None
    if cell_dir is None or not cell_dir.is_dir():
        raise ValueError(f"{library}/{cell} does not exist")
    sch_path = cell_dir / f"{cell}.sch"
    if not sch_path.is_file():
        raise ValueError(f"{library}/{cell} has no {cell}.sch")
    return sch_path, cell_dir


def capture_manual_cell(library: str, cell: str) -> dict[str, Any]:
    """The round-trip: headlessly render the cell's current .sch to a PNG and
    extract its SPICE netlist, then update the cell's provenance so
    resolve_schematic finds it for that topology from now on - the same
    filing outcome a successful Circuit_Builder run produces, just reached by
    hand-drawing in a live xschem session instead.

    Idiom validated by hand (see the xschem-schematic-authoring workflow
    notes) and re-confirmed here against this exact command form (2026-09):
      - PNG:     `xvfb-run -a xschem -q --png --plotfile <cell>.png <cell>.sch
                  --tcl "set enable_layer(15) 0; set enable_layer(17) 0"`
                  (NEVER -x/--no_x for this command - it blocks PNG export
                  even under Xvfb).
      - netlist: `xschem -x -q -n -s -o <cell_dir> -N <cell>.spice <cell>.sch`
                  - genuinely headless, needs no X/Xvfb at all for -x/-n.
    A netlist-extraction failure does NOT abort the capture (the PNG +
    provenance are what resolve_schematic needs); it's reported back as
    `netlist_warning` instead so the caller can surface it without treating
    the whole capture as failed.
    """
    sch_path, cell_dir = sch_and_cwd_for_cell(library, cell)

    _write_project_xschemrc(cell_dir)

    # xschem resolves the relative schematic-filename argument against its
    # OWN idea of "current directory" - which, empirically confirmed 2026-09,
    # is $env(PWD) (a plain string it reads, not a real getcwd() syscall),
    # NOT the OS-level working directory subprocess.Popen's cwd= sets. A
    # stale inherited PWD (e.g. the backend process's own launch directory)
    # silently sends xschem looking for the schematic THERE instead - it logs
    # "unable to open file: <wrong path>" but still exits 0 and still writes
    # an (empty) netlist file, so this is NOT optional: without it, a capture
    # can "succeed" while netlisting nothing. Always override PWD to the
    # cell dir for both subprocess calls below, independent of whatever PWD
    # the backend itself happens to have been started with.
    env = os.environ.copy()
    env["PWD"] = str(cell_dir)

    png_name = f"{cell}.png"
    log_path = cell_dir / "capture.log"
    render_cmd = [
        "xvfb-run", "-a", "xschem", "-q", "--png", "--plotfile", png_name,
        sch_path.name, "--tcl", "set enable_layer(15) 0; set enable_layer(17) 0",
    ]
    try:
        with open(log_path, "w") as logf:
            proc = subprocess.run(
                render_cmd, cwd=str(cell_dir), env=env, stdout=logf, stderr=subprocess.STDOUT, timeout=60
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CaptureFailed(f"could not render {cell}.sch to PNG: {exc}") from None
    render_log = log_path.read_text() if log_path.exists() else ""
    if proc.returncode != 0 or not (cell_dir / png_name).is_file() or "unable to open file" in render_log:
        raise CaptureFailed(f"PNG render failed (xschem exit {proc.returncode}): {render_log[-800:]}")

    spice_name = f"{cell}.spice"
    netlist_cmd = [
        "xschem", "-x", "-q", "-n", "-s", "-o", str(cell_dir), "-N", spice_name, sch_path.name,
    ]
    netlist_ok = False
    netlist_warning = None
    try:
        with open(log_path, "a") as logf:
            proc2 = subprocess.run(
                netlist_cmd, cwd=str(cell_dir), env=env, stdout=logf, stderr=subprocess.STDOUT, timeout=60
            )
        netlist_log = log_path.read_text()
        # xschem can exit 0 and still write a (wrongly empty) netlist file
        # after failing to open the source schematic - "unable to open
        # file" in the log is the only reliable tell, so treat it as a
        # failure even though returncode/file-exists both look fine.
        netlist_ok = (
            proc2.returncode == 0
            and (cell_dir / spice_name).is_file()
            and "unable to open file" not in netlist_log
        )
        if not netlist_ok:
            netlist_warning = (
                f"netlist extraction failed (xschem exit {proc2.returncode}) - see capture.log; "
                "the rendered PNG and provenance were still updated"
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        netlist_warning = f"netlist extraction failed to run: {exc}"

    if not netlist_ok:
        # Never leave a bogus (empty-subckt, wrong-file) netlist sitting on
        # disk looking like a real view - a stale one from a PREVIOUS
        # successful capture is fine to keep, but a fresh failed attempt's
        # output must not survive to be mistaken for real content.
        (cell_dir / spice_name).unlink(missing_ok=True)

    prov_path = cell_dir / "provenance.json"
    prov: dict[str, Any] = {}
    if prov_path.exists():
        try:
            prov = json.loads(prov_path.read_text())
        except (json.JSONDecodeError, OSError):
            prov = {}
    views = prov.get("views") or []
    for fname in (sch_path.name, png_name, spice_name if netlist_ok else None):
        if fname and fname not in views and (cell_dir / fname).is_file():
            views.append(fname)
    prov["views"] = views
    prov["filed_at"] = datetime.now(timezone.utc).isoformat()
    prov.setdefault("source", "manual")
    prov_path.write_text(json.dumps(prov, indent=2))

    return {
        "library": library, "cell": cell, "dir": str(cell_dir),
        "sch": sch_path.name, "png": png_name,
        "netlist": spice_name if netlist_ok else None,
        "netlist_warning": netlist_warning,
        "png_url": f"/api/libraries/{library}/{cell}/file/{png_name}",
    }
