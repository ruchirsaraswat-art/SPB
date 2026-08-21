"""Unit checks for backend/models_contract.py (cycle 2 + cycle-3 audit fixes).

Plain-python assert style (no pytest on this machine). Run with:
    backend/.venv/bin/python backend/tests/test_models_contract.py
Exits non-zero on the first failure; prints a per-check pass line otherwise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import models_contract as mc  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


# --- applicability matrix ---------------------------------------------------

ALL_TOPOLOGIES = set(mc._CONTINUOUS_ANALOG) | set(mc._CLOCKED_BIT) | {
    "output_driver", "ffe", "dfe", "custom"}
ok("matrix covers every topology + custom", set(mc.MODEL_APPLICABILITY) == ALL_TOPOLOGIES,
   str(set(mc.MODEL_APPLICABILITY) ^ ALL_TOPOLOGIES))
ok("21 catalog topologies + custom", len(mc.MODEL_APPLICABILITY) == 22,
   str(len(mc.MODEL_APPLICABILITY)))

# cycle-3 P3-9: output_driver is the one all-three-levels block
ok("output_driver offers all three levels",
   mc.supported_model_types("output_driver") == ["veriloga", "verilog", "rnm"])
for t in mc._CONTINUOUS_ANALOG:
    assert mc.supported_model_types(t) == ["veriloga", "rnm"], t
ok("continuous-analog blocks offer veriloga+rnm only", True)
for t in mc._CLOCKED_BIT:
    assert mc.supported_model_types(t) == ["verilog", "rnm"], t
ok("clocked blocks offer verilog+rnm only", True)
ok("ffe/dfe rnm-only", mc.supported_model_types("ffe") == ["rnm"]
   and mc.supported_model_types("dfe") == ["rnm"])
# cycle-3 item 13: ffe/dfe digital-Verilog denial has its own (tap-weight) reason
ok("ffe/dfe verilog denial mentions tap weights",
   "tap" in mc.MODEL_APPLICABILITY["ffe"]["verilog"]
   and mc.MODEL_APPLICABILITY["dfe"]["verilog"] == mc.MODEL_APPLICABILITY["ffe"]["verilog"])
ok("every denial is a human-readable string",
   all(v is True or (isinstance(v, str) and len(v) > 20)
       for m in mc.MODEL_APPLICABILITY.values() for v in m.values()))

# --- request validation -----------------------------------------------------

mc.validate_models_request("ctle", ["veriloga", "rnm"])
mc.validate_models_request("output_driver", ["veriloga", "verilog", "rnm"])
mc.validate_models_request("pll", None)
ok("valid requests accepted", True)
for topo, req in (("ctle", ["verilog"]), ("pll", ["veriloga"]), ("ffe", ["verilog"]),
                  ("ctle", ["nonsense"])):
    try:
        mc.validate_models_request(topo, req)
    except ValueError:
        pass
    else:
        ok(f"invalid request rejected ({topo}/{req})", False)
ok("invalid requests rejected", True)

# --- pass tokens ------------------------------------------------------------

toks = list(mc.PASS_TOKENS.values())
ok("no pass-token substring collisions",
   not any(a != b and a in b for a in toks for b in toks))

# --- prompt-contract text: cycle-3 P1 fixes ---------------------------------

va = mc.VA_RESTRICTIONS
ok("P1-1: corrected current-contribution pole idiom present",
   "I(int) <+ gain*V(in) - V(int) - ddt(V(int))/wp" in va)
ok("P1-1: correct implicit voltage form documented",
   "V(int) <+ gain*V(in) - ddt(V(int))/wp" in va)
ok("P1-1: old broken idiom gone", "V(int) <+ V(in)*gain - V(int)" not in va)

for topo in ("bias_current_mirror", "nmos_current_mirror"):
    g = mc._VA_GUIDANCE[topo]
    assert "tanh(3*V(out)/Vcompliance)" in g, topo
    assert "tanh(V(out)/compliance)" not in g, topo
    assert "80% of target" in g, topo  # rolloff clause (anti-degenerate)
ok("P1-2: mirror tanh(3V/Vc) + strengthened rolloff check in both mirrors", True)

rnm = mc.RNM_RULES
ok("P1-3: synchronous per-edge jitter for locked loops",
   "INDEPENDENTLY" in rnm and "do NOT use it" in rnm)
ok("P1-3: accumulation restricted to free-running oscillators",
   "free-running oscillators" in rnm)
pl = mc._RNM_GUIDANCE["phase_loop"]
ok("P1-3: phase_loop guidance prescribes synchronous injection",
   "INDEPENDENTLY" in pl and "do NOT accumulate" in pl)

# --- cycle-3 P2 fixes -------------------------------------------------------

ok("P2-4: LDO VA dropout unit conversion", "dropout_mv/1000.0" in mc._VA_GUIDANCE["ldo"])
ok("P2-4: RNM reference dropout unit conversion",
   "dropout_mv/1000.0" in mc._RNM_GUIDANCE["reference"])
ok("P2-5: RNM one-pole exponent units spelled out",
   "TS_s = TS_ps * 1e-12" in rnm and "SECONDS" in rnm)
ok("P2-6: RNM watchdog required",
   "watchdog" in rnm and "RNM_TB_FAIL watchdog timeout" in rnm)
ok("P2-6: Verilog watchdog required",
   "watchdog" in mc.VERILOG_RULES and "V_TB_FAIL watchdog timeout" in mc.VERILOG_RULES)
ok("P2-7: Verilog rules ban exact-equality timing checks",
   "never exact equality" in mc.VERILOG_RULES)
for topo, frag in (("pll", "+/- 2 fs"), ("comparator", "+/- 2 fs"),
                   ("sense_amp_slicer", "+/- 2 fs"), ("dll_phase_interpolator", "+/- 2 fs")):
    assert frag in mc._V_GUIDANCE[topo], topo
    assert "== 1/output_freq_ghz" not in mc._V_GUIDANCE[topo], topo
    assert "== code*step" not in mc._V_GUIDANCE[topo], topo
    assert "delay == parameter" not in mc._V_GUIDANCE[topo], topo
ok("P2-7: tolerance-based timing checks in V guidance", True)
ok("P2-7: RNM decision delay check tolerance-based",
   "+/- 2 fs" in mc._RNM_GUIDANCE["decision"]
   and "delay == parameter" not in mc._RNM_GUIDANCE["decision"])
for topo in ("ctle", "vga", "opamp_ota"):
    assert "Overdrive check" in mc._VA_GUIDANCE[topo], topo
ok("P2-8: overdrive/clamp check in VA amp-class guidance", True)
ok("P2-8: overdrive/saturation check in RNM amp class",
   "Overdrive check" in mc._RNM_GUIDANCE["amp"])

# --- cycle-3 P3 text fixes --------------------------------------------------

od = mc._VA_GUIDANCE["output_driver"]
ok("P3-9: output_driver VA guidance captures rout",
   "output_impedance_ohm" in od and "tanh" in od and "within 10%" in od)
ok("P3-10: bandgap PSRR exercised", "dVref/dVdd" in mc._VA_GUIDANCE["bandgap_reference"])
ok("P3-10: LDO PSRR exercised", "1 kHz" in mc._VA_GUIDANCE["ldo"]
   and "psrr_db_at_1khz" in mc._VA_GUIDANCE["ldo"])
op = mc._VA_GUIDANCE["opamp_ota"]
ok("P3-11: opamp slew transient check + PM disclaimer",
   "slew" in op and "large-step" in op and "PM = 90 deg by construction" in op)
ok("P3-12: comparator dead-band REQUIRED",
   "REQUIRED" in mc._RNM_GUIDANCE["decision"]
   and "optional" not in mc._RNM_GUIDANCE["decision"])
ok("P3-13: LDO RNM tau must be derived and documented",
   "DERIVES AND DOCUMENTS" in mc._RNM_GUIDANCE["reference"])
ok("P3-13: lock-time constant unified on 4/(2*pi*BW)",
   "4/(2*pi*loop_bandwidth)" in mc._V_GUIDANCE["pll"]
   and "4/(2*pi*loop_BW)" in mc._RNM_GUIDANCE["phase_loop"]
   and "5/(2*pi" not in mc._V_GUIDANCE["pll"])
ok("P3-13: $dist_normal seed-copy instruction", "integer` variable" in rnm)
ok("P3-13: RNM amp pole measurement method stated",
   "do NOT" in mc._RNM_GUIDANCE["amp"] and "FFT" in mc._RNM_GUIDANCE["amp"])

# --- deliverable filenames --------------------------------------------------

ok("deliverable filenames fixed per contract",
   mc.deliverable_files("x", "veriloga") == {"file": "x.va", "testbench": "x_va_tb.spice", "sim_log": "va_sim.log"}
   and mc.deliverable_files("x", "verilog") == {"file": "x.v", "testbench": "x_v_tb.v", "sim_log": "v_sim.log"}
   and mc.deliverable_files("x", "rnm") == {"file": "x_rnm.sv", "testbench": "x_rnm_tb.sv", "sim_log": "rnm_sim.log"})

# --- prompt section assembly ------------------------------------------------

sec = mc.build_models_prompt_section("nmos_current_mirror", ["veriloga", "rnm"])
for frag in ("tanh(3*V(out)/Vcompliance)", "I(int) <+ gain*V(in)", "pre_osdi",
             "VA_TB_PASS", "RNM_TB_PASS", "watchdog", "TS_s = TS_ps * 1e-12",
             '"models"', "generated_unverified"):
    assert frag in sec, frag
ok("mirror veriloga+rnm prompt section carries corrected contract", True)
ok("empty request yields empty section", mc.build_models_prompt_section("ctle", []) == "")
sec_od = mc.build_models_prompt_section("output_driver", ["veriloga", "verilog", "rnm"])
ok("output_driver 3-level prompt section builds",
   "output_impedance_ohm" in sec_od and "### Digital Verilog" in sec_od)

# --- honesty pass -----------------------------------------------------------

def entry(**kw):
    e = {"file": "b.va", "testbench": "b_va_tb.spice", "sim_log": "va_sim.log",
         "status": "verified", "status_reason": "", "checks": {}}
    e.update(kw)
    return e


with tempfile.TemporaryDirectory() as td:
    rd = Path(td)

    # missing requested type
    s = {"models": {}}
    probs = mc.check_models_in_summary(s, rd, ["rnm"])
    ok("honesty: missing requested type becomes failed entry",
       s["models"]["rnm"]["status"] == "failed" and probs)

    # claimed file missing on disk
    s = {"models": {"veriloga": entry()}}
    probs = mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: claimed file missing on disk -> failed",
       s["models"]["veriloga"]["status"] == "failed" and "missing" in probs[0])

    (rd / "b.va").write_text("// model")
    (rd / "b_va_tb.spice").write_text("* tb")

    # verified claim without .osdi (cycle-3 item 13)
    s = {"models": {"veriloga": entry()}}
    probs = mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: veriloga verified without .osdi -> failed",
       s["models"]["veriloga"]["status"] == "failed" and "b.osdi" in probs[0])

    (rd / "b.osdi").write_bytes(b"\x00")

    # verified claim without pass token in log
    s = {"models": {"veriloga": entry()}}
    probs = mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: verified without pass token -> failed",
       s["models"]["veriloga"]["status"] == "failed")

    # verified with fail token alongside pass token -> failed
    (rd / "va_sim.log").write_text("VA_TB_PASS\nVA_TB_FAIL oops\n")
    s = {"models": {"veriloga": entry()}}
    mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: pass+fail tokens together -> failed",
       s["models"]["veriloga"]["status"] == "failed")

    # fully backed verified claim survives
    (rd / "va_sim.log").write_text("... VA_TB_PASS\n")
    s = {"models": {"veriloga": entry()}}
    probs = mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: backed verified claim survives",
       s["models"]["veriloga"]["status"] == "verified" and not probs)

    # compile_only without .osdi -> failed; with it -> survives
    (rd / "b.osdi").unlink()
    s = {"models": {"veriloga": entry(status="compile_only")}}
    mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: compile_only without .osdi -> failed",
       s["models"]["veriloga"]["status"] == "failed")
    (rd / "b.osdi").write_bytes(b"\x00")
    s = {"models": {"veriloga": entry(status="compile_only")}}
    mc.check_models_in_summary(s, rd, ["veriloga"])
    ok("honesty: compile_only with .osdi survives",
       s["models"]["veriloga"]["status"] == "compile_only")

    # generated_unverified rnm passes through untouched
    (rd / "b_rnm.sv").write_text("// rnm")
    (rd / "b_rnm_tb.sv").write_text("// tb")
    s = {"models": {"rnm": {"file": "b_rnm.sv", "testbench": "b_rnm_tb.sv",
                            "sim_log": None, "status": "generated_unverified",
                            "status_reason": "iverilog not installed", "checks": {}}}}
    probs = mc.check_models_in_summary(s, rd, ["rnm"])
    ok("honesty: generated_unverified passes through",
       s["models"]["rnm"]["status"] == "generated_unverified" and not probs)

print(f"\nALL {CHECKS} CHECKS PASSED")
