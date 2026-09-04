"""Unit checks for backend/schematics.py's "draw a new schematic by hand"
feature (create_blank_cell / capture_manual_cell / sch_and_cwd_for_cell).

Plain-python assert style (no pytest on this machine), same convention as
tests/test_schematics.py. Exercises the REAL xschem binary for the capture
step (like test_va_fixture.py does for openvaf) - a machine without xschem/
xvfb-run installed skips those two checks rather than failing the whole file,
matching the tool's own graceful-degradation stance for this feature.

Run with:
    backend/.venv/bin/python backend/tests/test_manual_schematic.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_td = tempfile.TemporaryDirectory()
TMP = Path(_td.name)
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(TMP / "settings.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as st  # noqa: E402
import schematics as sm  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


work = TMP / "work"
work.mkdir()
st.set_working_dir(str(work))

# --- create_blank_cell -------------------------------------------------------

created = sm.create_blank_cell("ddr_afe", "bandgap_reference", "bandgap_reference", label="Vref Gen")
sch_path = Path(created["dir"]) / created["sch"]
ok("blank cell created at libraries/<lib>/<cell>/<cell>.sch",
   sch_path == st.libraries_root() / "ddr_afe" / "bandgap_reference" / "bandgap_reference.sch",
   str(sch_path))
ok("blank .sch has the real xschem header (v/G/K/V/S/E)",
   sch_path.read_text().splitlines()[:6] == ["v {xschem version=3.4.4 file_version=1.2}",
                                              "G {}", "K {}", "V {}", "S {}", "E {}"])

prov = json.loads((sch_path.parent / "provenance.json").read_text())
ok("provenance records the topology", prov.get("topology") == "bandgap_reference", str(prov))
ok("provenance records the manual source", prov.get("source") == "manual", str(prov))
ok("provenance has no filed_at yet (no PNG -> resolve_schematic must not find it)",
   not prov.get("filed_at"), str(prov))
ok("xschemrc written into the new cell dir (sky130 symbols resolve immediately)",
   (sch_path.parent / "xschemrc").is_file())

# --- refuses to clobber an existing .sch ------------------------------------

try:
    sm.create_blank_cell("ddr_afe", "bandgap_reference", "bandgap_reference")
    ok("duplicate create raises CellExists", False)
except sm.CellExists as exc:
    ok("duplicate create raises CellExists", exc.library == "ddr_afe" and exc.cell == "bandgap_reference", str(exc))

ok("original .sch untouched by the refused duplicate create",
   sch_path.read_text().splitlines()[:6] == ["v {xschem version=3.4.4 file_version=1.2}",
                                              "G {}", "K {}", "V {}", "S {}", "E {}"])

# A cell dir that exists with OTHER views (no .sch) is extended, not blocked.
other_cell_dir = st.libraries_root() / "ddr_afe" / "odt_only_symbol"
other_cell_dir.mkdir(parents=True)
(other_cell_dir / "odt_only_symbol.sym").write_text("v {xschem}\n")
sm.create_blank_cell("ddr_afe", "odt_only_symbol", "input_termination_buffer")
ok("cell dir with only a prior .sym is extended with a new .sch, not blocked",
   (other_cell_dir / "odt_only_symbol.sch").is_file())
ok("the prior .sym view is untouched", (other_cell_dir / "odt_only_symbol.sym").is_file())

# --- name/topology validation propagates ------------------------------------

for bad_lib in ("", "../etc", "1bad", "a/b"):
    try:
        sm.create_blank_cell(bad_lib, "cell", "bandgap_reference")
        ok(f"bad library name {bad_lib!r} rejected", False)
    except ValueError:
        pass
ok("bad library names rejected", True)

try:
    sm.create_blank_cell("ddr_afe", "new_cell", "../etc")
    ok("bad topology name rejected", False)
except ValueError:
    pass
ok("bad topology name rejected", True)

# --- sch_and_cwd_for_cell -----------------------------------------------------

path, cwd = sm.sch_and_cwd_for_cell("ddr_afe", "bandgap_reference")
ok("sch_and_cwd_for_cell resolves the freshly-created cell (no PNG needed)",
   path == sch_path and cwd == sch_path.parent, str((path, cwd)))

try:
    sm.sch_and_cwd_for_cell("ddr_afe", "does_not_exist")
    ok("sch_and_cwd_for_cell on a missing cell raises ValueError", False)
except ValueError:
    pass
ok("sch_and_cwd_for_cell on a missing cell raises ValueError", True)

# --- resolve_schematic must NOT find an uncaptured manual cell --------------

resolved = sm.resolve_schematic("bandgap_reference", [], libs_dir=st.libraries_root(), runs_dir=[])
ok("uncaptured manual cell -> resolve_schematic reports found=false",
   resolved["found"] is False, str(resolved))

# --- capture_manual_cell (real xschem binary) -------------------------------

_have_toolchain = all(shutil.which(b) for b in ("xschem", "xvfb-run", "Xvfb"))
if not _have_toolchain:
    print("SKIP capture_manual_cell checks - xschem/xvfb-run/Xvfb not on PATH")
else:
    captured = sm.capture_manual_cell("ddr_afe", "bandgap_reference")
    ok("capture wrote a PNG", (sch_path.parent / captured["png"]).is_file(), str(captured))
    ok("capture extracted a netlist", captured.get("netlist") == "bandgap_reference.spice", str(captured))
    ok("capture reports no netlist warning on success", captured.get("netlist_warning") is None, str(captured))

    prov2 = json.loads((sch_path.parent / "provenance.json").read_text())
    ok("provenance filed_at set after capture", bool(prov2.get("filed_at")), str(prov2))
    ok("provenance views include sch+png+spice after capture",
       set(prov2.get("views") or []) >= {"bandgap_reference.sch", "bandgap_reference.png", "bandgap_reference.spice"},
       str(prov2))

    resolved2 = sm.resolve_schematic("bandgap_reference", [], libs_dir=st.libraries_root(), runs_dir=[])
    ok("captured manual cell now resolves as a library candidate",
       resolved2["found"] is True and resolved2["source"] == "library"
       and resolved2["library"] == "ddr_afe" and resolved2["cell"] == "bandgap_reference",
       str(resolved2))
    ok("resolved png_url points at the library file endpoint",
       resolved2["png_url"] == "/api/libraries/ddr_afe/bandgap_reference/file/bandgap_reference.png",
       resolved2.get("png_url"))

    # Capturing again (e.g. after further edits) must not fail or lose the
    # earlier-recorded label.
    captured_again = sm.capture_manual_cell("ddr_afe", "bandgap_reference")
    ok("re-capture succeeds", captured_again.get("netlist") == "bandgap_reference.spice", str(captured_again))
    prov3 = json.loads((sch_path.parent / "provenance.json").read_text())
    ok("re-capture preserves the original label", prov3.get("label") == "Vref Gen", str(prov3))

    # Regression guard (2026-09): capture_manual_cell's netlist step must
    # read the REAL, current .sch content - not silently netlist nothing.
    # A blank schematic's netlist is indistinguishable from a "couldn't find
    # the file" failure (both are an empty **.subckt/.ends/.end - exactly
    # what masked this bug the first time), so this check writes a real
    # device into the .sch by hand (standing in for what a saved-from-xschem
    # file would contain) and asserts the netlist actually contains it.
    sm.create_blank_cell("ddr_afe", "populated_check", "input_termination_buffer")
    populated_dir = st.libraries_root() / "ddr_afe" / "populated_check"
    (populated_dir / "populated_check.sch").write_text(
        "v {xschem version=3.4.4 file_version=1.2}\n"
        "G {}\nK {}\nV {}\nS {}\nE {}\n"
        "C {sky130_fd_pr/nfet_01v8.sym} 300 -200 0 0 "
        "{name=M1 L=1 W=1 nf=1 mult=1 model=nfet_01v8 spiceprefix=X}\n"
    )
    captured_pop = sm.capture_manual_cell("ddr_afe", "populated_check")
    ok("populated capture reports success", captured_pop.get("netlist_warning") is None, str(captured_pop))
    pop_netlist = (populated_dir / "populated_check.spice").read_text()
    ok("populated netlist actually contains the placed device (not an empty subckt)",
       "nfet_01v8" in pop_netlist, pop_netlist)
    pop_log = (populated_dir / "capture.log").read_text()
    ok("capture.log shows no 'unable to open file' (PWD-resolution regression)",
       "unable to open file" not in pop_log, pop_log)

try:
    sm.capture_manual_cell("ddr_afe", "does_not_exist")
    ok("capture on a missing cell raises ValueError", False)
except ValueError:
    pass
ok("capture on a missing cell raises ValueError", True)

print(f"\nall {CHECKS} checks passed")
