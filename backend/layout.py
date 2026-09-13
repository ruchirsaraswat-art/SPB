"""
Layout sub-window support (2026-09): the magic counterpart to
backend/schematics.py's xschem support. Same library/cell/view convention
(libraries/<lib>/<cell>/<cell>.mag alongside <cell>.sch/<cell>.sym) and the
same two actions - "create a blank layout and open it live" for a cell with
no layout yet, and "open the existing layout live" for one that has one -
just for magic instead of xschem.

WHY A SEPARATE MODULE rather than folding this into schematics.py: layouts
are their own view of a cell, not a schematic concept, and keeping the
split mirrors the existing xschem_session.py/schematics.py split (generic
live-session ENGINE vs. per-view-type resolve/create logic). The session
ENGINE itself (Xvfb + x11vnc + the authenticated WebSocket proxy + the idle
reaper + the concurrency cap + orphan cleanup) is NOT duplicated here - see
backend/xschem_session.py's register_kind()/_KIND_SPAWNERS: this module
registers "magic" once at import time and then calls that module's
start_session()/stop_session()/restart_session()/list_sessions() exactly
like schematics.py's xschem-session call sites do, so the concurrency cap
and idle reaper are shared across xschem AND magic sessions for free.

UNLIKE schematics.py, there is deliberately NO "resolve across filed cells
vs. latest run" priority search: Circuit_Builder does not produce layouts
today, so there is no "run" source to search - a layout only ever exists as
a filed library cell's <cell>.mag, found or not.

HISTORY (2026-09, both superseded - kept here only so the reasoning behind
`-rcfile` below still makes sense): this module was originally written
against the distro package's magic 8.3.105, which had two real defects -
it SEGFAULTED on `quit` in `-dnull` batch mode, and a freshly-opened cell
bound to a placeholder "minimum" technology instead of "sky130A" even
though the tech database loaded fine - so it shipped with NO capture/render
step at all (batch magic was categorically unreliable). The machine was
then upgraded to a source build, magic 8.3.683 (GCC 15's C23 default had
broken 8.3.105's K&R-style declarations; rebuilt with `-std=gnu17`), which
fixes BOTH: `-dnull` batch `quit` now exits cleanly, the tech file loads
with zero errors, and a freshly-opened/painted cell correctly reports and
saves as "sky130A" (re-verified by hand: `paint nwell` after loading a
blank sky130A-tagged cell produced a real `<< nwell >>` rect in the saved
file, and `[tech name]` reported "sky130A", not "minimum"). capture_manual_
layout() below - added AFTER the upgrade - is real, exercises the actual
magic binary, and is covered by tests/test_layout_session.py's real-binary
checks (skipped gracefully, like test_manual_schematic.py's xschem
equivalent, if magic/xvfb-run aren't on PATH) - it is not a
speculatively-enabled feature.
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import xschem_session
from settings import all_libraries_roots

REQUIRED_BINARIES = ("Xvfb", "magic", "x11vnc")
FEATURE_LABEL = "Interactive layout (magic)-in-browser"

EMPTY_STATE_MESSAGE = "No layout yet for this cell."


# --- magic-specific spawn config (registered with xschem_session's shared ---
# --- session engine - see that module's register_kind()) --------------------


def _magicrc_content(width: int, height: int) -> str:
    """Content for the ONE rcfile passed via `magic -rcfile <this>` for a
    session - see _spawn_magic for why -rcfile (not magic's own default
    system(~/.magicrc)+design(./.magicrc) two-stage lookup, and NOT a
    project-local .magicrc dropped into the cell dir the way
    _write_project_xschemrc does for xschem).

    EMPIRICALLY CONFIRMED (2026-09, building this feature) that the default
    two-stage lookup is broken on this magic 8.3.105 install: the instant
    BOTH a system (~/.magicrc) AND a local (cwd/.magicrc) rcfile exist -
    regardless of what the local one even contains, a blank file reproduces
    it too - magic errors sourcing the local one ("can't rename to
    \"closewrapperonly\": command already exists") and silently skips it
    ("Bad local startup file \".magicrc\", continuing without"). Non-fatal,
    but it would make this function's addpath/window-resize commands never
    run if written into the cell dir the xschemrc way. Passing a single
    file via `-rcfile` bypasses that lookup entirely (verified: no such
    error, and `wm geometry` genuinely resizes the layout1 window - see
    layout_session tests) - the ONLY rcfile magic reads is this one, so it
    must do the full tech load itself, same as the real ~/.magicrc does."""
    lib_lines = "".join(f"addpath {{{root}}}\n" for root in all_libraries_roots())
    return (
        "# Session-local magicrc (2026-09 layout-editor integration) - see\n"
        "# backend/layout.py's _magicrc_content docstring for why this is\n"
        "# passed via `-rcfile` instead of relying on magic's own default\n"
        "# system+design rcfile lookup (empirically broken on this install).\n"
        "if {![info exists env(PDK_ROOT)]} { set env(PDK_ROOT) $env(HOME)/.volare }\n"
        "source $env(PDK_ROOT)/sky130A/libs.tech/magic/sky130A.magicrc\n"
        "# This tool's own design libraries (lib/cell/view tree - backend/\n"
        "# libraries.py), so cells filed by previous runs/sessions are\n"
        "# reachable for hierarchical layouts, same idea as xschem's\n"
        "# XSCHEM_LIBRARY_PATH (see circuit_builder._write_project_xschemrc).\n"
        + lib_lines
        + "# magic's Tk toplevel (window name \"layout1\") does not fill the\n"
        "# display either (same story as xschem - no window manager on these\n"
        "# throwaway Xvfb displays) - resize it the moment it exists. Polls\n"
        "# rather than a single fixed delay (more robust under load than the\n"
        "# fixed 500ms this was first verified working with).\n"
        "proc _analog_tool_resize_layout_window {} {\n"
        f"    if {{![catch {{wm geometry .layout1 {width}x{height}+0+0}}]}} {{ return }}\n"
        "    after 200 _analog_tool_resize_layout_window\n"
        "}\n"
        "after 200 _analog_tool_resize_layout_window\n"
    )


def _spawn_magic(
    mag_path: Path, cwd: Path, display_num: int, log_path: Path, resolution: str
) -> subprocess.Popen:
    """Launch interactive magic on `mag_path`, DISPLAY=:<display_num>,
    resized to fill that display. NEVER `-dnull` (that's the batch/headless
    device this tool never uses - see the module docstring) and NEVER
    `-T sky130A` (verified failure: magic can't find sky130A.tech via its
    own tech-name search path, which knows nothing about $PDK_ROOT/.volare -
    the tech loads via the rcfile's `source .../sky130A.magicrc` chain
    instead, exactly like the user's own real ~/.magicrc does it).

    `-noconsole`: no separate text-console window - keeps the ONLY
    meaningful window a VNC viewer needs to show to exactly `layout1` (magic
    also opens a real but 1x1 `magicexec` helper window regardless of this
    flag - harmless, not something this tool manages, matches the verified
    fact that it coexists fine with a working interactive session)."""
    env = os.environ.copy()
    env["DISPLAY"] = f":{display_num}"
    w, h = xschem_session.validate_resolution(resolution)
    session_dir = log_path.parent
    rcfile = session_dir / "project.magicrc"
    rcfile.write_text(_magicrc_content(w, h))
    with open(log_path, "w") as logf:
        return subprocess.Popen(
            ["magic", "-rcfile", str(rcfile), "-noconsole", str(mag_path)],
            cwd=str(cwd),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


xschem_session.register_kind(
    "magic",
    spawn_app=_spawn_magic,
    required_binaries=REQUIRED_BINARIES,
    feature_label=FEATURE_LABEL,
    write_project_rc=None,  # see _magicrc_content's docstring - no cwd-resident rcfile for magic
)


# --- thin wrappers over the shared session engine ---------------------------
# Same shape as backend/xschem_session.py's own public functions - main.py's
# /api/layout/* routes call these exactly like /api/xschem/* calls
# xschem_session directly, just pre-bound to kind="magic" and (for
# restart/get/list) filtered/guarded to only ever touch magic sessions.


def prereqs_status() -> dict[str, Any]:
    return xschem_session.prereqs_status_for_kind("magic")


def start_session(
    cwd: Path, mag_path: Path, label: str, resolution: str = xschem_session.DEFAULT_RESOLUTION
) -> dict[str, Any]:
    return xschem_session.start_session(cwd, mag_path, label, resolution=resolution, kind="magic")


def stop_session(session_id: str) -> bool:
    return xschem_session.stop_session(session_id)


def restart_session(session_id: str, resolution: str) -> dict[str, Any]:
    info = xschem_session.get_session(session_id)
    if info is None:
        raise xschem_session.LaunchFailed(f"session {session_id} not found - it may have already been stopped")
    if info.get("kind") != "magic":
        raise xschem_session.LaunchFailed(f"session {session_id} is not a layout (magic) session")
    return xschem_session.restart_session(session_id, resolution)


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    info = xschem_session.get_session(session_id)
    return info if info is not None and info.get("kind") == "magic" else None


def list_sessions() -> list[dict[str, Any]]:
    return xschem_session.list_sessions(kind="magic")


# --- resolve / create (mirrors schematics.py's manual-cell helpers) ---------


def resolve_layout(library: str, cell: str) -> dict[str, Any]:
    """Whether <library>/<cell> has a filed <cell>.mag - a plain file check,
    not a priority search (see module docstring: layouts only ever come
    from a filed library cell, never a Circuit_Builder run)."""
    from libraries import find_library_dir, validate_library_name

    validate_library_name(library)
    validate_library_name(cell)
    lib_dir = find_library_dir(library)
    cell_dir = (lib_dir / cell) if lib_dir else None
    if cell_dir is None or not cell_dir.is_dir():
        return {"library": library, "cell": cell, "found": False, "message": EMPTY_STATE_MESSAGE}
    mag_path = cell_dir / f"{cell}.mag"
    if not mag_path.is_file():
        return {"library": library, "cell": cell, "found": False, "message": EMPTY_STATE_MESSAGE}
    png_path = cell_dir / f"{cell}_layout.png"  # see capture_manual_layout's comment on the "_layout" suffix
    result: dict[str, Any] = {
        "library": library,
        "cell": cell,
        "found": True,
        "dir": str(cell_dir),
        "mag": mag_path.name,
    }
    if png_path.is_file():
        result["png"] = png_path.name
        result["png_url"] = f"/api/libraries/{library}/{cell}/file/{png_path.name}"
    return result


CAPTURE_PLOT_SIZE = 900  # pixels - matches xschem's capture PNG being a reasonable preview size


class CaptureFailed(RuntimeError):
    pass


def _pnm_p6_to_png(pnm_bytes: bytes) -> bytes:
    """Pure-stdlib (zlib only - no Pillow/new dependency) binary P6 PPM ->
    PNG encoder. magic's `plot pnm` command emits exactly this format
    (verified empirically 2026-09: header `P6\\n<w> <h>\\n255\\n` followed by
    w*h RGB triples, one byte per channel)."""
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", pnm_bytes)
    if not m:
        raise CaptureFailed("magic's plot output is not the expected binary P6 PPM format")
    w, h, maxval = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if maxval != 255:
        raise CaptureFailed(f"unexpected PNM maxval {maxval} (expected 255)")
    pixels = pnm_bytes[m.end():]
    stride = w * 3
    if len(pixels) < stride * h:
        raise CaptureFailed("PNM pixel data shorter than its declared dimensions")

    def chunk(tag: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # PNG filter type 0 (None) for each scanline
        raw.extend(pixels[y * stride:(y + 1) * stride])
    compressed = zlib.compress(bytes(raw), 6)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit depth, color type 2 (truecolor RGB)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def capture_manual_layout(library: str, cell: str) -> dict[str, Any]:
    """Render the cell's current .mag to a PNG via magic's batch `plot pnm`,
    converted with _pnm_p6_to_png (no new dependency) - the layout
    counterpart to schematics.capture_manual_cell's PNG half (there is no
    netlist-extraction counterpart - out of scope, see the module
    docstring). Real graphics device via `xvfb-run` (NOT `-dnull`) -
    empirically required (2026-09): `select top cell`'s automatic
    plot-bounding-box computation only works against a real (even
    off-screen) device; under `-dnull` the box silently stays at its
    default and the plot comes out blank. Runs in a throwaway tmp dir (the
    rcfile + intermediate .pnm are never filed alongside the cell's real
    views) and never touches the source .mag file itself. Raises
    CaptureFailed on any failure (missing binaries, magic exiting non-zero,
    an unparseable plot output) - callers turn that into a 500, not a
    crash."""
    mag_path, cell_dir = mag_and_cwd_for_cell(library, cell)
    # "_layout.png" (NOT "<cell>.png") - the schematic view's rendered PNG
    # already owns that canonical name (schematics._pick_view picks
    # "<cell>.png" first) and a cell commonly has BOTH a schematic and a
    # layout view; writing to the same name would silently clobber whichever
    # was captured first.
    png_name = f"{cell}_layout.png"
    log_path = cell_dir / "layout_capture.log"

    with tempfile.TemporaryDirectory(prefix="analog_tool_layout_capture_") as tmp:
        tmp_dir = Path(tmp)
        rcfile = tmp_dir / "capture.magicrc"
        rcfile.write_text(_magicrc_content(CAPTURE_PLOT_SIZE, CAPTURE_PLOT_SIZE))
        pnm_path = tmp_dir / f"{cell}.pnm"
        script = f"select top cell\nplot pnm {pnm_path} {CAPTURE_PLOT_SIZE}\nquit -noprompt\n"
        cmd = ["xvfb-run", "-a", "magic", "-rcfile", str(rcfile), "-noconsole", mag_path.name]
        try:
            with open(log_path, "w") as logf:
                proc = subprocess.run(
                    cmd, cwd=str(cell_dir), input=script.encode(), stdout=logf,
                    stderr=subprocess.STDOUT, timeout=60,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise CaptureFailed(f"could not render {cell}.mag: {exc}") from None
        if proc.returncode != 0 or not pnm_path.is_file():
            log_tail = log_path.read_text()[-800:] if log_path.exists() else ""
            raise CaptureFailed(f"magic plot failed (exit {proc.returncode}): {log_tail}")
        png_bytes = _pnm_p6_to_png(pnm_path.read_bytes())
        (cell_dir / png_name).write_bytes(png_bytes)

    prov_path = cell_dir / "provenance.json"
    prov: dict[str, Any] = {}
    if prov_path.exists():
        try:
            prov = json.loads(prov_path.read_text())
        except (json.JSONDecodeError, OSError):
            prov = {}
    views = prov.get("views") or []
    if png_name not in views:
        views.append(png_name)
    prov["views"] = views
    prov["layout_captured_at"] = datetime.now(timezone.utc).isoformat()
    prov_path.write_text(json.dumps(prov, indent=2))

    return {
        "library": library, "cell": cell, "dir": str(cell_dir),
        "png": png_name, "png_url": f"/api/libraries/{library}/{cell}/file/{png_name}",
    }


def mag_and_cwd_for_cell(library: str, cell: str) -> tuple[Path, Path]:
    """A specific library cell's .mag path + cwd, without launching
    anything. ValueError when the library/cell doesn't exist or has no
    .mag - callers turn that into a 404/422, not a crash."""
    from libraries import find_library_dir, validate_library_name

    validate_library_name(library)
    validate_library_name(cell)
    lib_dir = find_library_dir(library)
    cell_dir = (lib_dir / cell) if lib_dir else None
    if cell_dir is None or not cell_dir.is_dir():
        raise ValueError(f"{library}/{cell} does not exist")
    mag_path = cell_dir / f"{cell}.mag"
    if not mag_path.is_file():
        raise ValueError(f"{library}/{cell} has no {cell}.mag")
    return mag_path, cell_dir


# Minimal-but-valid blank magic layout: the same 3-field-plus-terminator
# shape as the smallest real sky130 reference cells (e.g.
# sky130A/libs.ref/sky130_ml_xx_hd/mag/font_20.mag: `magic` / `tech sky130A`
# / `timestamp` / `<< end >>`), just without that file's optional
# `magscale`/properties lines - empirically confirmed (2026-09, building
# this feature) to open cleanly (no parse errors, "Creating new cell", a
# real layout1 window) under `magic -rcfile ... <this file>`. See the
# module docstring's KNOWN LIMITATION note: this install may still bind the
# freshly-opened cell to a placeholder "minimum" technology rather than
# sky130A despite the file correctly declaring it - a separate, deeper
# issue this module does not attempt to paper over.
_BLANK_MAG_TEMPLATE = "magic\ntech sky130A\ntimestamp 0\n<< end >>\n"


class LayoutExists(RuntimeError):
    """Refused: `library`/`cell` already has a .mag - the caller must offer
    "open it" instead of silently clobbering someone's existing layout."""

    def __init__(self, library: str, cell: str):
        self.library = library
        self.cell = cell
        super().__init__(
            f"{library}/{cell} already has a layout - open the existing one instead "
            "of creating a new cell with the same name"
        )


def create_blank_layout(
    library: str, cell: str, topology: str, label: Optional[str] = None
) -> dict[str, Any]:
    """Create libraries/<library>/<cell>/<cell>.mag as a blank-but-valid
    magic layout. Never overwrites an existing .mag (raises LayoutExists); a
    cell dir that already exists with OTHER views (e.g. it already has a
    schematic/symbol) is extended in place, not blocked - this is the same
    cell/view convention _every_ view type in this tool shares."""
    from libraries import create_library, find_library_dir, validate_library_name

    validate_library_name(library)
    validate_library_name(cell)
    from schematics import validate_topology_name

    validate_topology_name(topology)

    lib_dir = find_library_dir(library)
    if lib_dir is None:
        create_library(library)
        lib_dir = find_library_dir(library)
    cell_dir = lib_dir / cell
    mag_path = cell_dir / f"{cell}.mag"
    if mag_path.is_file():
        raise LayoutExists(library, cell)

    cell_dir.mkdir(parents=True, exist_ok=True)
    mag_path.write_text(_BLANK_MAG_TEMPLATE)

    prov_path = cell_dir / "provenance.json"
    prov: dict[str, Any] = {}
    existing_cell = prov_path.exists()
    if existing_cell:
        try:
            prov = json.loads(prov_path.read_text())
        except (json.JSONDecodeError, OSError):
            prov = {}
            existing_cell = False
    prov.setdefault("topology", topology)
    if label:
        prov.setdefault("label", label)
    # "source"/"created_at" describe how the CELL came to exist, not how this
    # one view did. Adding a hand-drawn .mag to a cell Circuit_Builder
    # generated must not relabel it: a generated cell carries no "source" key
    # at all, so a setdefault here would silently stamp it "manual" and reset
    # its creation date. Only a genuinely new cell (no provenance yet) gets
    # them. Adding the .mag to "views" is correct either way.
    if not existing_cell:
        prov["source"] = "manual"
        prov["created_at"] = datetime.now(timezone.utc).isoformat()
    views = prov.get("views") or []
    if mag_path.name not in views:
        views.append(mag_path.name)
    prov["views"] = views
    prov_path.write_text(json.dumps(prov, indent=2))

    return {
        "library": library, "cell": cell, "dir": str(cell_dir),
        "mag": mag_path.name, "topology": topology,
    }
