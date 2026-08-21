"""
Virtuoso-style library/cell/view organization for build results (user
request, cycle 2 addendum).

How this maps onto xschem's actual "database" model (checked on this
machine): xschem has NO design database - it is purely directory-based.
XSCHEM_LIBRARY_PATH (set in xschemrc) is a colon-separated list of directory
roots; every directory under any root shows up as a "library" in xschem's
file browser, and cells are just .sch/.sym files referenced by a path
relative to some root (e.g. `sky130_fd_pr/nfet_01v8.sym`). The sky130 PDK's
own xschem "library" is exactly that: a directory of .sym files.

So the Virtuoso lib -> cell -> view hierarchy is imposed here as a directory
convention that xschem happily browses:

    libraries/                      <- LIBS_DIR, appended to every run's
      <library>/                       XSCHEM_LIBRARY_PATH (see
        <cell>/                        circuit_builder._write_project_xschemrc)
          <cell>.sch                <- "schematic" view
          <cell>.sym                <- "symbol" view (when Circuit_Builder made one)
          <cell>.spice              <- "netlist" view
          <cell>_tb.spice           <- "testbench" view
          <cell>.png                <- rendered schematic image
          <block>.va / _rnm.sv / .v <- behavioral-model views (cycle 2)
          summary.json, sim.log ... <- measurement record
          provenance.json           <- which run produced this cell, when

A cell filed this way is instantiable from any future xschem session started
in a run directory (its symbol resolves as `<library>/<cell>/<cell>.sym`).

Filing happens on successful runs only, by COPYING from the immutable
runs/<run_id>/ directory - the run dir stays the untouched audit record,
the library is the curated, reusable design area. Re-filing the same
library/cell overwrites the cell's views (latest run wins), with the
previous run's id still recoverable from run history.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from settings import all_libraries_roots, libraries_root

# Same charset rules a filesystem/xschem path needs; also blocks traversal.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")

# View files copied from a run dir into the cell, by summary/artifact key.
_ARTIFACT_KEYS = (
    "schematic_file", "schematic_png", "netlist_file",
    "testbench_file", "testbench_sch_file", "sim_log_file",
)


def validate_library_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise ValueError(
            f"invalid library name {name!r} - use letters, digits, '_' or '-', "
            "starting with a letter or '_', max 64 chars (it becomes a directory "
            "name on XSCHEM_LIBRARY_PATH)"
        )


def _view_kind(fname: str) -> str:
    """Rough Virtuoso-view-style label for a file, for the API/UI listing."""
    low = fname.lower()
    if low.endswith("_tb.spice") or low.endswith("_tb.sch"):
        return "testbench"
    if low.endswith(".sch"):
        return "schematic"
    if low.endswith(".sym"):
        return "symbol"
    if low.endswith(".spice") or low.endswith(".cir"):
        return "netlist"
    if low.endswith(".png"):
        return "image"
    if low.endswith(".va") or low.endswith(".osdi"):
        return "veriloga"
    if low.endswith(".sv"):
        return "rnm"
    if low.endswith(".v"):
        return "verilog"
    if low.endswith(".log"):
        return "log"
    if low == "summary.json":
        return "measurements"
    if low == "provenance.json":
        return "provenance"
    return "other"


def list_libraries() -> list[dict[str, Any]]:
    """All libraries with their cells and per-cell view files, searched
    across the current libraries root and any previous working dirs' roots
    (newest-config first - see backend/settings.py; the first root a library
    name appears in wins, so a library recreated at the current location
    shadows its old-location namesake). Directory scan on every call -
    library counts here are tiny (this is a one-designer local tool), so no
    caching/index to go stale, and settings changes apply with no restart."""
    libs = []
    seen: set[str] = set()
    for root in all_libraries_roots():
        if not root.is_dir():
            continue
        for lib_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if lib_dir.name in seen:
                continue
            seen.add(lib_dir.name)
            cells = []
            for cell_dir in sorted(p for p in lib_dir.iterdir() if p.is_dir()):
                views = [
                    {"file": f.name, "kind": _view_kind(f.name)}
                    for f in sorted(cell_dir.iterdir())
                    if f.is_file()
                ]
                prov = {}
                prov_path = cell_dir / "provenance.json"
                if prov_path.exists():
                    try:
                        prov = json.loads(prov_path.read_text())
                    except (json.JSONDecodeError, OSError):
                        prov = {}
                cells.append({
                    "name": cell_dir.name,
                    "views": views,
                    "run_id": prov.get("run_id"),
                    "topology": prov.get("topology"),
                    "filed_at": prov.get("filed_at"),
                })
            libs.append({"name": lib_dir.name, "cells": cells, "root": str(root)})
    libs.sort(key=lambda l: l["name"])
    return libs


def find_library_dir(name: str) -> Optional[Path]:
    """Where an existing library actually lives: current root first, then
    previous roots (newest-config first). None if it exists nowhere."""
    validate_library_name(name)
    for root in all_libraries_roots():
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def create_library(name: str) -> dict[str, Any]:
    """Create an empty library directory under the CURRENT libraries root
    (resolved from settings at call time). Idempotent-safe: a library that
    already exists anywhere on the search path is reported as such rather
    than errored or shadowed, since the user's intent ('make sure this
    library exists') is satisfied."""
    validate_library_name(name)
    existing = find_library_dir(name)
    if existing is not None:
        return {
            "name": name,
            "created": False,
            "already_existed": True,
            "path": str(existing),
        }
    lib_dir = libraries_root() / name
    lib_dir.mkdir(parents=True, exist_ok=True)
    return {"name": name, "created": True, "already_existed": False, "path": str(lib_dir)}


def file_run_into_library(
    library: str,
    run_id: str,
    run_dir: Path,
    spec: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Copy a successful run's design views into libraries/<library>/<cell>/.

    Cell name = the schematic file's base name (what Circuit_Builder actually
    called the circuit), falling back to the topology value. Returns
    {"library", "cell", "views": [copied filenames]}. Raises ValueError on a
    bad library name; IO problems propagate to the caller, which records
    them on the run rather than pretending the filing happened.
    """
    validate_library_name(library)
    schematic = summary.get("schematic_file") or ""
    cell = Path(schematic).stem if schematic else (spec.get("topology") or "cell")
    # File into wherever the library actually lives (it may predate a
    # working-dir change and still sit under a previous root - keep its
    # cells together); a genuinely new library lands under the current root.
    lib_dir = find_library_dir(library) or (libraries_root() / library)
    cell_dir = lib_dir / cell
    cell_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    def _copy(fname: str | None) -> None:
        if not fname:
            return
        src = run_dir / fname
        if src.is_file() and fname not in copied:
            shutil.copy2(src, cell_dir / fname)
            copied.append(fname)

    for key in _ARTIFACT_KEYS:
        _copy(summary.get(key))
    # Any symbol Circuit_Builder drew for the cell (makes it instantiable
    # from other schematics - the whole point of a library).
    for sym in run_dir.glob("*.sym"):
        _copy(sym.name)
    # Behavioral-model views (cycle 2): model + testbench + sim log per type.
    for entry in (summary.get("models") or {}).values():
        if isinstance(entry, dict):
            for key in ("file", "testbench", "sim_log"):
                _copy(entry.get(key))
            # Compiled OSDI object alongside a Verilog-A view, so the filed
            # cell is usable via `pre_osdi` without recompiling.
            mfile = entry.get("file") or ""
            if mfile.endswith(".va"):
                _copy(Path(mfile).stem + ".osdi")
    _copy("summary.json")

    (cell_dir / "provenance.json").write_text(json.dumps({
        "run_id": run_id,
        "topology": spec.get("topology"),
        "chosen_architecture": spec.get("chosen_architecture"),
        "label": spec.get("label"),
        "filed_at": datetime.now(timezone.utc).isoformat(),
        "views": copied,
    }, indent=2))
    copied.append("provenance.json")

    return {"library": library, "cell": cell, "views": copied, "path": str(cell_dir)}
