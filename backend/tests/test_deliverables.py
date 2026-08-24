"""Unit checks for the single-deliverable run modes (Generate dropdown):
prompt branches, RunSpec applicability 422s, per-mode result evaluation, and
single-deliverable library filing.

Plain-python assert style (matching tests/test_models_contract.py). Run with:
    backend/.venv/bin/python backend/tests/test_deliverables.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate settings (working dir) into a temp tree BEFORE importing modules
# that resolve roots from it, so filing tests never touch real libraries.
_TMP = tempfile.TemporaryDirectory()
_settings_file = Path(_TMP.name) / "settings.json"
_workdir = Path(_TMP.name) / "wd"
_workdir.mkdir()
_settings_file.write_text(json.dumps({"working_dir": str(_workdir)}))
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(_settings_file)

import circuit_builder as cb  # noqa: E402
import libraries as lib  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


MIRROR = dict(
    topology="nmos_current_mirror", iref_ua=10, ratio_out=2, ratio_ref=1,
    w_ref_um=2.0, l_ref_um=0.5, vdd_v=1.8, corner="tt", temp_c=27,
)
CTLE = dict(
    topology="ctle", vdd_v=1.8, corner="tt", temp_c=27,
    extra_fields={"dc_gain_db": 0, "peaking_db": 6, "data_rate_gbps": 2},
)

# --- prompt branches --------------------------------------------------------

full = cb.build_prompt({**MIRROR})
ok("full (no deliverable key) unchanged: verification suite present",
   "Run that testbench in ngspice batch mode" in full and "summary.json" in full)
ok("full identical when deliverable='full'", cb.build_prompt({**MIRROR, "deliverable": "full"}) == full)

sch = cb.build_prompt({**MIRROR, "deliverable": "schematic_only"})
sch_n = " ".join(sch.split())  # prompts hard-wrap; normalize for multi-word checks
ok("schematic_only: scope line present", "SCHEMATIC-ONLY RUN" in sch)
ok("schematic_only: skips verification suite",
   "Do NOT build the verification simulation suite" in sch_n)
ok("schematic_only: quick DC sanity allowed", "dc_sanity.log" in sch_n and "`.op`" in sch)
ok("schematic_only: no behavioral models", "do NOT write any behavioral models" in sch_n)
ok("schematic_only: still draws + netlists + renders",
   ".sch" in sch_n and "xschem -x -q -n -s" in sch_n and "PNG" in sch)
ok("schematic_only: summary contract has schematic keys",
   '"schematic_png"' in sch_n and '"dc_log_file"' in sch_n and '"deliverable": "schematic_only"' in sch)
ok("schematic_only: mirror spec values present", "Iref = 10 uA" in sch_n and "2:1" in sch)

sch_g = cb.build_prompt({**CTLE, "deliverable": "schematic_only",
                         "chosen_architecture": "RC-degenerated CTLE"})
ok("schematic_only generic: extra fields + chosen architecture flow through",
   "Peaking" in sch_g and "RC-degenerated CTLE" in sch_g)

sym = cb.build_prompt({**MIRROR, "deliverable": "symbol"})
sym_n = " ".join(sym.split())
ok("symbol: scope is .sym only", "SYMBOL-ONLY RUN" in sym and ".sym" in sym)
ok("symbol: no filed sch -> spec-derived pins",
   "no filed schematic exists" in sym_n.lower() and '"spec_derived"' in sym)
ok("symbol: cheap netlist smoke check requested", "symbol_check.sch" in sym)
ok("symbol: no simulation", "no SPICE simulation" in sym_n)

sym2 = cb.build_prompt({**MIRROR, "deliverable": "symbol",
                        "_symbol_cell": "current_mirror",
                        "_symbol_source_sch": "current_mirror.sch"})
ok("symbol with filed sch: pins from the copied schematic",
   "current_mirror.sch" in sym2 and '"filed_schematic"' in sym2
   and "derive the symbol's pins EXACTLY" in " ".join(sym2.split()))
ok("symbol with filed sch: symbol filename = cell name", "`current_mirror.sym`" in sym2)

for mtype, token in (("veriloga", "VA_TB_PASS"), ("rnm", "RNM_TB_PASS")):
    p = cb.build_prompt({**MIRROR, "deliverable": mtype})
    assert "MODEL-ONLY RUN" in p and "NO transistor-level design" in p, mtype
    assert token in p, mtype
    assert '"models"' in p, mtype
    # standalone framing: no "actually achieved" (that references SPICE
    # measurements of a design this run never does)
    assert "ACTUALLY ACHIEVED" not in p, mtype
    assert "IN ADDITION" not in p, mtype
ok("model-only prompts: standalone framing + same pass-token contract", True)

pv = cb.build_prompt({**CTLE, "deliverable": "veriloga"})
ok("model-only reuses per-type contract text verbatim (VA restrictions)",
   "Mandatory Verilog-A coding restrictions" in pv and "pre_osdi" in pv)
pverilog = cb.build_prompt({**MIRROR, "topology": "pll", "deliverable": "verilog",
                            "extra_fields": {"output_freq_ghz": 2.5}})
ok("verilog model-only prompt builds for a clocked block",
   "V_TB_PASS" in pverilog and "MODEL-ONLY RUN" in pverilog)

# full-run models section unchanged (not standalone)
import models_contract as mc  # noqa: E402
sec = mc.build_models_prompt_section("ctle", ["veriloga"])
ok("full-run models section still uses in-addition framing",
   "IN ADDITION" in sec and "ACTUALLY ACHIEVED" in sec)
sec_alone = mc.build_models_prompt_section("ctle", ["veriloga"], standalone=True)
ok("standalone section shares the per-type block verbatim",
   sec.split("### Verilog-A model")[1] == sec_alone.split("### Verilog-A model")[1])

# --- timeouts / light-run classification ------------------------------------

# DEFAULT_TIMEOUT_S was trimmed 2400 -> 1800 (Token_Optimizer audit item 4,
# 2026-08-23), so schematic_only (1800) now EQUALS the full-run timeout.
ok("timeouts: full = trimmed default (1800), light modes shorter",
   cb.timeout_for(None) == cb.DEFAULT_TIMEOUT_S
   and cb.DEFAULT_TIMEOUT_S == 1800
   and cb.timeout_for("full") == cb.DEFAULT_TIMEOUT_S
   and cb.timeout_for("symbol") < cb.timeout_for("rnm") < cb.timeout_for("schematic_only") <= cb.DEFAULT_TIMEOUT_S)
ok("light deliverables = symbol + model types",
   set(cb.LIGHT_DELIVERABLES) == {"symbol", "veriloga", "verilog", "rnm"})

# --- RunSpec validation (422s) ----------------------------------------------

main.RunSpec(**MIRROR, deliverable="rnm", library="lib_a")
main.RunSpec(**MIRROR, deliverable="symbol")
main.RunSpec(**CTLE, deliverable="veriloga")
ok("valid deliverable specs accepted", True)
ok("default deliverable is full (behavior unchanged)",
   main.RunSpec(**CTLE).deliverable == "full")

for kwargs, frag in (
    ({**CTLE, "deliverable": "verilog"}, "not applicable"),          # no digital verilog for analog CTLE
    ({**MIRROR, "topology": "pll", "extra_fields": None, "deliverable": "veriloga"}, "not applicable"),
    ({**CTLE, "deliverable": "schematic_only", "models": ["rnm"]}, "deliverable='full'"),
    ({**CTLE, "deliverable": "rnm", "models": ["veriloga"]}, "deliverable='full'"),
    ({**CTLE, "deliverable": "bogus"}, ""),                           # unknown literal
):
    try:
        main.RunSpec(**kwargs)
    except ValidationError as e:
        if frag:
            assert frag in str(e), (kwargs, str(e))
    else:
        ok(f"invalid spec rejected ({kwargs.get('deliverable')})", False)
ok("invalid deliverable specs rejected with the applicability reasons", True)

# Through the HTTP layer: a model-only request for an inapplicable topology
# is a 422 (invalid specs never create a run dir, so this is side-effect
# free).
client = TestClient(main.app)
r = client.post("/api/runs", json={**CTLE, "deliverable": "verilog", "library": "lib_a"})
ok("API: 422 for inapplicable model-only deliverable", r.status_code == 422,
   str(r.status_code))
ok("API: 422 body carries the checkbox reason string",
   "no bit-level behavior" in json.dumps(r.json()))
r = client.post("/api/runs", json={**CTLE, "deliverable": "full", "models": ["verilog"], "library": "lib_a"})
ok("API: checkbox path 422 unchanged", r.status_code == 422)
r = client.post("/api/runs", json={**CTLE, "deliverable": "symbol", "models": ["rnm"], "library": "lib_a"})
ok("API: 422 for models + non-full deliverable", r.status_code == 422)

# --- evaluate_summary per mode ----------------------------------------------

with tempfile.TemporaryDirectory() as td:
    rd = Path(td)

    # schematic_only: success requires the PNG on disk
    summary = {"status": "success", "schematic_png": "x.png", "schematic_file": "x.sch"}
    st, reason, arts = cb.evaluate_summary({"deliverable": "schematic_only"}, rd, dict(summary))
    ok("schematic_only: claimed-but-missing PNG -> failed", st == "failed" and "missing" in reason)
    (rd / "x.png").write_bytes(b"\x89PNG")
    (rd / "x.sch").write_text("v {}")
    st, reason, arts = cb.evaluate_summary({"deliverable": "schematic_only"}, rd, dict(summary))
    ok("schematic_only: succeeds with sch+png on disk", st == "success" and arts["schematic_png"] == "x.png")

    # symbol: no PNG requirement, but the .sym must exist
    s_sym = {"status": "success", "symbol_file": "cell.sym"}
    st, reason, _ = cb.evaluate_summary({"deliverable": "symbol"}, rd, dict(s_sym))
    ok("symbol: missing .sym -> failed", st == "failed")
    (rd / "cell.sym").write_text("v {}")
    st, reason, arts = cb.evaluate_summary({"deliverable": "symbol"}, rd, dict(s_sym))
    ok("symbol: succeeds with .sym on disk (no PNG needed)",
       st == "success" and arts["symbol_file"] == "cell.sym")

    # model-only: a failed model fails the RUN (unlike full runs)
    (rd / "b_rnm.sv").write_text("// m")
    (rd / "b_rnm_tb.sv").write_text("// tb")
    m_entry = {"file": "b_rnm.sv", "testbench": "b_rnm_tb.sv", "sim_log": "rnm_sim.log",
               "status": "verified", "status_reason": "", "checks": {}}
    s_m = {"status": "success", "models": {"rnm": dict(m_entry)}}
    st, reason, _ = cb.evaluate_summary({"deliverable": "rnm"}, rd, s_m)
    ok("model-only: verified claim without pass token fails the run",
       st == "failed" and s_m["models"]["rnm"]["status"] == "failed")
    (rd / "rnm_sim.log").write_text("RNM_TB_PASS\n")
    s_m = {"status": "success", "models": {"rnm": dict(m_entry)}}
    st, reason, _ = cb.evaluate_summary({"deliverable": "rnm"}, rd, s_m)
    ok("model-only: backed verified claim succeeds (no schematic required)", st == "success")
    # missing models key entirely -> failed run
    s_m = {"status": "success"}
    st, reason, _ = cb.evaluate_summary({"deliverable": "rnm"}, rd, s_m)
    ok("model-only: missing models key -> failed run", st == "failed")

    # full run: model failure still only downgrades the model (regression)
    (rd / "full.png").write_bytes(b"\x89PNG")
    s_f = {"status": "success", "schematic_png": "full.png",
           "models": {"rnm": {**m_entry, "sim_log": "nolog.log"}}}
    st, reason, _ = cb.evaluate_summary({"deliverable": "full", "models": ["rnm"]}, rd, s_f)
    ok("full: model problem does not fail the run (unchanged)",
       st == "success" and s_f["models"]["rnm"]["status"] == "failed")

# --- single-deliverable library filing --------------------------------------

libs_root = _workdir / "libraries"
cell_dir = libs_root / "lib_a" / "my_mirror"
cell_dir.mkdir(parents=True)
(cell_dir / "my_mirror.sch").write_text("v {}")
(cell_dir / "my_mirror.png").write_bytes(b"\x89PNG")
(cell_dir / "provenance.json").write_text(json.dumps({
    "run_id": "run_full", "topology": "nmos_current_mirror",
    "filed_at": "2026-08-20T00:00:00+00:00",
    "views": ["my_mirror.sch", "my_mirror.png"],
}))

ok("find_cell_for_topology finds the filed cell",
   lib.find_cell_for_topology("lib_a", "nmos_current_mirror") == "my_mirror")
ok("find_cell_for_topology: no match -> None",
   lib.find_cell_for_topology("lib_a", "pll") is None)

with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    (run_dir / "nmos_current_mirror_rnm.sv").write_text("// m")
    (run_dir / "nmos_current_mirror_rnm_tb.sv").write_text("// tb")
    (run_dir / "rnm_sim.log").write_text("RNM_TB_PASS\n")
    (run_dir / "summary.json").write_text("{}")
    summary = {"models": {"rnm": {"file": "nmos_current_mirror_rnm.sv",
                                  "testbench": "nmos_current_mirror_rnm_tb.sv",
                                  "sim_log": "rnm_sim.log", "status": "verified"}}}
    spec = {"topology": "nmos_current_mirror", "deliverable": "rnm", "library": "lib_a"}
    filed = lib.file_run_into_library("lib_a", "run_rnm", run_dir, spec, summary)
    ok("model-only filing lands in the EXISTING cell for the topology",
       filed["cell"] == "my_mirror")
    ok("model views copied into the cell",
       (cell_dir / "nmos_current_mirror_rnm.sv").is_file()
       and (cell_dir / "rnm_sim.log").is_file())
    prov = json.loads((cell_dir / "provenance.json").read_text())
    ok("provenance updated to the model run + deliverable recorded",
       prov["run_id"] == "run_rnm" and prov["deliverable"] == "rnm")
    ok("provenance keeps the earlier schematic views (merge, not overwrite)",
       "my_mirror.sch" in prov["views"] and "nmos_current_mirror_rnm.sv" in prov["views"])

    # symbol filing: symbol_file view lands in the same cell
    (run_dir / "my_mirror.sym").write_text("v {}")
    sym_summary = {"symbol_file": "my_mirror.sym", "cell": "my_mirror"}
    filed = lib.file_run_into_library("lib_a", "run_sym", run_dir,
                                      {"topology": "nmos_current_mirror", "deliverable": "symbol"},
                                      sym_summary)
    ok("symbol filing: .sym view added to the same cell",
       filed["cell"] == "my_mirror" and (cell_dir / "my_mirror.sym").is_file())

# --- _prepare_symbol_inputs -------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    prep = cb._prepare_symbol_inputs(
        {"topology": "nmos_current_mirror", "library": "lib_a"}, run_dir)
    ok("symbol prep: filed schematic copied into the run dir",
       prep == {"_symbol_cell": "my_mirror", "_symbol_source_sch": "my_mirror.sch"}
       and (run_dir / "my_mirror.sch").is_file())
    prep = cb._prepare_symbol_inputs({"topology": "pll", "library": "lib_a"}, run_dir)
    ok("symbol prep: no filed cell -> spec-derived, cell named after topology",
       prep == {"_symbol_cell": "pll", "_symbol_source_sch": None})
    prep = cb._prepare_symbol_inputs({"topology": "pll", "library": None}, run_dir)
    ok("symbol prep: no library chosen -> spec-derived", prep["_symbol_source_sch"] is None)

# --- arch_model deliverable -------------------------------------------------

ARCH = {
    "blocks": [
        {"id": "rx-pad", "label": "RX PAD", "topology": None},
        {"id": "rx-frontend", "label": "RX Term/Buf", "topology": "input_termination_buffer"},
        {"id": "rx-eq", "label": "RX Equalizer", "topology": "ctle"},
        {"id": "sampler", "label": "Sampler", "topology": "sense_amp_slicer"},
        {"id": "cdr", "label": "CDR", "topology": "cdr"},
    ],
    "edges": [
        {"from": "rx-pad", "to": "rx-frontend"}, {"from": "rx-frontend", "to": "rx-eq"},
        {"from": "rx-eq", "to": "sampler"}, {"from": "cdr", "to": "sampler"},
    ],
}
ARCH_SPEC = dict(deliverable="arch_model", architecture=ARCH, arch_model_type="rnm",
                 phy_type="ser-des", vdd_v=1.8, corner="tt", temp_c=27)

main.RunSpec(**ARCH_SPEC, library="lib_a")
main.RunSpec(**{**ARCH_SPEC, "arch_model_type": "verilog", "arch_blocks": ["sampler", "cdr"]})
ok("arch_model: valid specs accepted (rnm full diagram; verilog clocked subset)", True)
for kwargs, frag in (
    ({**ARCH_SPEC, "arch_model_type": "verilog"}, "not applicable to every included block"),
    ({**ARCH_SPEC, "arch_blocks": ["nope"]}, "not present in the architecture"),
    ({**ARCH_SPEC, "arch_blocks": ["rx-pad"]}, "none of the included blocks is designable"),
    ({**ARCH_SPEC, "architecture": None}, "requires architecture"),
    ({**ARCH_SPEC, "models": ["rnm"]}, "do not apply to an arch_model run"),
    ({"topology": "ctle", "architecture": ARCH}, "only meaningful with deliverable='arch_model'"),
):
    try:
        main.RunSpec(**kwargs)
    except ValidationError as e:
        assert frag in str(e), (frag, str(e)[:200])
    else:
        ok(f"arch_model invalid spec rejected ({frag})", False)
ok("arch_model: invalid specs rejected with reasons", True)
r = client.post("/api/runs", json={**ARCH_SPEC, "arch_model_type": "verilog"})
ok("API: 422 for verilog arch model over analog blocks", r.status_code == 422)

pa = cb.build_prompt(ARCH_SPEC)
pa_n = " ".join(pa.split())
ok("arch prompt: scope + no transistor design",
   "ARCHITECTURE-MODEL RUN" in pa and "no transistor-level design" in pa_n)
ok("arch prompt: per-block files + guidance",
   "rx_eq_rnm.sv" in pa and "sampler_rnm.sv" in pa
   and "What this block's model must capture" in pa)
ok("arch prompt: pads excluded, edges listed",
   "rx-pad" in pa and "rx-frontend -> rx-eq" in pa)
ok("arch prompt: top module + tokens + rules",
   "ser_des_arch" in pa and "ARCH_TB_PASS" in pa and "timescale 1ps/1fs" in pa)
files = cb.arch_deliverable_files(ARCH_SPEC)
ok("arch files contract", files["top_file"] == "ser_des_arch_rnm.sv"
   and files["sim_log_file"] == "arch_sim.log"
   and set(files["block_files"]) == {"rx-frontend", "rx-eq", "sampler", "cdr"})
ok("arch timeout defined, not a light run",
   cb.timeout_for("arch_model") == 1800 and "arch_model" not in cb.LIGHT_DELIVERABLES)

with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    s_a = {"status": "success", "deliverable": "arch_model", "model_type": "rnm",
           "cell": "ser_des_arch", "top_file": "ser_des_arch_rnm.sv",
           "block_files": {"rx-eq": "rx_eq_rnm.sv"},
           "testbench_file": "ser_des_arch_tb.sv", "sim_log_file": "arch_sim.log",
           "model_status": "verified", "status_reason": "", "blocks_included": ["rx-eq"]}
    st, reason, _ = cb.evaluate_summary({"deliverable": "arch_model"}, rd, dict(s_a))
    ok("arch eval: missing files -> failed", st == "failed" and "missing" in reason)
    for f in ("ser_des_arch_rnm.sv", "rx_eq_rnm.sv", "ser_des_arch_tb.sv"):
        (rd / f).write_text("// stub")
    (rd / "arch_sim.log").write_text("no token here\n")
    s2 = dict(s_a)
    st, reason, _ = cb.evaluate_summary({"deliverable": "arch_model"}, rd, s2)
    ok("arch eval: verified claim without ARCH_TB_PASS -> failed",
       st == "failed" and s2["model_status"] == "failed")
    (rd / "arch_sim.log").write_text("ARCH_TB_PASS\n")
    s3 = dict(s_a)
    st, reason, arts = cb.evaluate_summary({"deliverable": "arch_model"}, rd, s3)
    ok("arch eval: backed verified claim succeeds",
       st == "success" and arts["top_file"] == "ser_des_arch_rnm.sv")

    # arch filing: cell from summary.cell, block files copied
    filed = lib.file_run_into_library("lib_a", "run_arch", rd,
                                      {"topology": "custom", "deliverable": "arch_model"}, s3)
    ok("arch filing: cell named after top module, block files copied",
       filed["cell"] == "ser_des_arch"
       and (libs_root / "lib_a" / "ser_des_arch" / "rx_eq_rnm.sv").is_file()
       and (libs_root / "lib_a" / "ser_des_arch" / "ser_des_arch_rnm.sv").is_file())

# --- explicit target cell (library cell picker) ------------------------------

ok("RunSpec accepts a target cell",
   main.RunSpec(**CTLE, cell="my_mirror").cell == "my_mirror")
try:
    main.RunSpec(**CTLE, cell="../evil")
except ValidationError:
    ok("bad cell name rejected", True)
else:
    ok("bad cell name rejected", False)

sch_cell = cb.build_prompt({**CTLE, "deliverable": "schematic_only", "cell": "my_ctle"})
ok("schematic_only prompt names the targeted cell",
   "Name the schematic exactly `my_ctle.sch`" in " ".join(sch_cell.split()))
full_cell = cb.build_prompt({**CTLE, "cell": "my_ctle"})
ok("full generic prompt names the targeted cell",
   "Name the schematic exactly `my_ctle.sch`" in " ".join(full_cell.split()))

with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    (rd / "whatever.sch").write_text("v {}")
    filed = lib.file_run_into_library(
        "lib_a", "run_cellpick", rd,
        {"topology": "ctle", "cell": "my_mirror"},  # user-picked target cell
        {"schematic_file": "whatever.sch"})
    ok("explicit cell overrides schematic-stem naming for filing",
       filed["cell"] == "my_mirror" and (libs_root / "lib_a" / "my_mirror" / "whatever.sch").is_file())

with tempfile.TemporaryDirectory() as td:
    run_dir = Path(td)
    prep = cb._prepare_symbol_inputs(
        {"topology": "nmos_current_mirror", "library": "lib_a", "cell": "my_mirror"}, run_dir)
    ok("symbol prep honors the explicitly-targeted cell",
       prep["_symbol_cell"] == "my_mirror" and prep["_symbol_source_sch"] == "my_mirror.sch")

# --- arch_stitch deliverable (top-level design stitch) -----------------------

# Give the test library's cell a symbol view so it is stitchable.
(cell_dir / "my_mirror.sym").write_text("v {xschem}\nstub\n")
STITCH_ARCH = {
    "blocks": [
        {"id": "rx-pad", "topology": None},
        {"id": "bias", "label": "Bias Mirror", "topology": "nmos_current_mirror"},
        {"id": "ctle1", "label": "CTLE", "topology": "ctle"},
    ],
    "edges": [{"from": "rx-pad", "to": "ctle1"}, {"from": "bias", "to": "ctle1"}],
}
STITCH_BASE = dict(deliverable="arch_stitch", architecture=STITCH_ARCH,
                   phy_type="ser-des", vdd_v=1.8, corner="tt")

main.RunSpec(**STITCH_BASE, library="lib_a", cell="serdes_top",
             arch_blocks=["bias"], arch_cell_map={"bias": "lib_a/my_mirror"})
ok("arch_stitch: valid spec accepted", True)
for kwargs, frag in (
    ({**STITCH_BASE, "library": "lib_a", "arch_blocks": ["bias"]}, "no library cell mapped"),
    ({**STITCH_BASE, "library": "lib_a", "arch_blocks": ["bias"],
      "arch_cell_map": {"bias": "lib_a/nope"}}, "does not exist"),
    ({**STITCH_BASE, "arch_blocks": ["bias"],
      "arch_cell_map": {"bias": "lib_a/my_mirror"}}, "requires a destination library"),
    ({**STITCH_BASE, "library": "lib_a", "arch_blocks": ["ctle1"],
      "arch_cell_map": {"ctle1": "lib_a/my_mirror2"}}, "does not exist"),
):
    try:
        main.RunSpec(**kwargs)
    except ValidationError as e:
        assert frag in str(e), (frag, str(e)[:250])
    else:
        ok(f"arch_stitch invalid spec rejected ({frag})", False)
ok("arch_stitch: invalid specs rejected (map/destination problems listed)", True)

# schematic-only cell (no .sym) is VALID: the stitch run generates the symbol
# from the filed .sch (user request) and it is filed back afterwards.
(cell_dir2 := libs_root / "lib_a" / "bare_cell").mkdir(parents=True, exist_ok=True)
(cell_dir2 / "provenance.json").write_text(json.dumps(
    {"topology": "ctle", "filed_at": "x", "views": ["bare_cell.sch"]}))
(cell_dir2 / "bare_cell.sch").write_text("v {}")
main.RunSpec(**STITCH_BASE, library="lib_a", arch_blocks=["ctle1"],
             arch_cell_map={"ctle1": "lib_a/bare_cell"})
ok("arch_stitch: schematic-only cell accepted (symbol generated in-run)", True)
# a cell with NEITHER view is still rejected
(cell_dir3 := libs_root / "lib_a" / "empty_cell").mkdir(parents=True, exist_ok=True)
(cell_dir3 / "provenance.json").write_text(json.dumps({"topology": "ctle", "filed_at": "x"}))
try:
    main.RunSpec(**STITCH_BASE, library="lib_a", arch_blocks=["ctle1"],
                 arch_cell_map={"ctle1": "lib_a/empty_cell"})
except ValidationError as e:
    ok("arch_stitch: cell with neither view rejected",
       "neither a symbol view" in str(e))
else:
    ok("arch_stitch: cell with neither view rejected", False)

# prompt: symbol-generation section + local instantiation + prep .sch copy
sp_gen = {**STITCH_BASE, "library": "lib_a", "cell": "gen_top",
          "arch_blocks": ["ctle1"], "arch_cell_map": {"ctle1": "lib_a/bare_cell"}}
pgen = cb.build_prompt(sp_gen)
ok("stitch prompt: missing symbol generated first",
   "Symbols to generate FIRST" in pgen and "`bare_cell.sym`" in pgen
   and '"symbols_generated": {"bare_cell": "bare_cell.sym"}' in pgen)
ok("stitch prompt: generated symbol instantiated locally, not via library path",
   "you generate in step 1" in pgen and "lib_a/bare_cell/bare_cell.sym" not in pgen)
with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    cb._prepare_stitch_inputs(sp_gen, rd)
    ok("stitch prep copies the schematic of symbol-less cells",
       (rd / "bare_cell.sch").is_file())
    cb._prepare_stitch_inputs({**sp_gen, "arch_blocks": ["bias"],
                               "arch_cell_map": {"bias": "lib_a/my_mirror"}}, rd)
    ok("stitch prep copies nothing for cells that already have symbols",
       not (rd / "my_mirror.sch").is_file())

# evaluate: claimed generated symbol must exist on disk
with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    s_gs = {"status": "success", "deliverable": "arch_stitch", "cell": "gen_top",
            "schematic_file": "gen_top.sch", "schematic_png": "gen_top.png",
            "netlist_file": "gen_top.spice",
            "blocks_instantiated": {"ctle1": "lib_a/bare_cell"},
            "symbols_generated": {"bare_cell": "bare_cell.sym"}}
    for f in ("gen_top.sch", "gen_top.spice"):
        (rd / f).write_text("v {}")
    (rd / "gen_top.png").write_bytes(b"\x89PNG")
    st, reason, _ = cb.evaluate_summary({"deliverable": "arch_stitch"}, rd, dict(s_gs))
    ok("stitch eval: claimed generated symbol missing -> failed",
       st == "failed" and "bare_cell.sym" in reason)
    (rd / "bare_cell.sym").write_text("v {}")
    st, reason, _ = cb.evaluate_summary({"deliverable": "arch_stitch"}, rd, dict(s_gs))
    ok("stitch eval: succeeds with generated symbol on disk", st == "success")

    # back-filing: generated symbol lands in the block's OWN cell + provenance
    filed = lib.file_generated_symbols({"ctle1": "lib_a/bare_cell"},
                                       {"bare_cell": "bare_cell.sym"}, rd)
    prov = json.loads((cell_dir2 / "provenance.json").read_text())
    ok("generated symbol filed back into the block's cell",
       filed == ["lib_a/bare_cell/bare_cell.sym"]
       and (cell_dir2 / "bare_cell.sym").is_file()
       and "bare_cell.sym" in prov["views"] and "bare_cell.sch" in prov["views"])
    ok("back-filing skips unknown/missing entries gracefully",
       lib.file_generated_symbols({"x": "lib_a/nope"}, {"nope": "ghost.sym"}, rd) == [])

sp = {**STITCH_BASE, "library": "lib_a", "cell": "serdes_top",
      "arch_blocks": ["bias"], "arch_cell_map": {"bias": "lib_a/my_mirror"}}
pst = cb.build_prompt(sp)
pst_n = " ".join(pst.split())
ok("stitch prompt: scope + no block-internal design + no sim",
   "STITCH-ONLY RUN" in pst and "Do NOT design or modify any block internals" in pst_n)
ok("stitch prompt: instantiates the mapped filed symbol",
   "lib_a/my_mirror/my_mirror.sym" in pst)
ok("stitch prompt: top cell named per the user's destination answer",
   "serdes_top.sch" in pst and '"cell": "serdes_top"' in pst)
ok("stitch prompt: placement guidance present",
   "left-to-right in signal-flow order" in pst_n)
ok("stitch prompt: pads become top-level pins",
   "labeled top-level pin" in pst_n)
ok("stitch timeout defined, not light",
   cb.timeout_for("arch_stitch") == 1200 and "arch_stitch" not in cb.LIGHT_DELIVERABLES)

with tempfile.TemporaryDirectory() as td:
    rd = Path(td)
    s_st = {"status": "success", "deliverable": "arch_stitch", "cell": "serdes_top",
            "schematic_file": "serdes_top.sch", "schematic_png": "serdes_top.png",
            "netlist_file": "serdes_top.spice",
            "blocks_instantiated": {"bias": "lib_a/my_mirror"}, "top_ports": ["RX_IN"]}
    st, reason, _ = cb.evaluate_summary({"deliverable": "arch_stitch"}, rd, dict(s_st))
    ok("stitch eval: missing files -> failed", st == "failed")
    for f in ("serdes_top.sch", "serdes_top.spice"):
        (rd / f).write_text("v {}")
    (rd / "serdes_top.png").write_bytes(b"\x89PNG")
    st, reason, arts = cb.evaluate_summary({"deliverable": "arch_stitch"}, rd, dict(s_st))
    ok("stitch eval: succeeds with sch+png+netlist on disk",
       st == "success" and arts["schematic_png"] == "serdes_top.png")
    # filing goes to the user-chosen destination cell (spec.cell override)
    filed = lib.file_run_into_library("lib_a", "run_stitch", rd,
                                      {"topology": "custom", "deliverable": "arch_stitch",
                                       "cell": "serdes_top"}, s_st)
    ok("stitch filing: lands at the chosen destination cell",
       filed["cell"] == "serdes_top"
       and (libs_root / "lib_a" / "serdes_top" / "serdes_top.sch").is_file())

print(f"\nALL {CHECKS} CHECKS PASSED")
