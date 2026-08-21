"""End-to-end fixture proof of the corrected Verilog-A pole idiom (cycle 3).

Compiles fixtures/onepole.va with the project-local openvaf, runs the
self-checking ngspice TB, and re-parses the measured numbers here as an
independent check: DC gain must be 20 dB (the old broken idiom gives ~14 dB)
and the -3 dB point must sit at 1 MHz (old idiom: ~2 MHz).

Run with: backend/.venv/bin/python backend/tests/test_va_fixture.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from models_contract import augmented_path  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ENV = dict(os.environ, PATH=augmented_path(os.environ.get("PATH", "")))


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    sys.exit(1)


with tempfile.TemporaryDirectory() as td:
    wd = Path(td)
    shutil.copy(FIXTURES / "onepole.va", wd / "onepole.va")
    shutil.copy(FIXTURES / "onepole_va_tb.spice", wd / "onepole_va_tb.spice")

    c = subprocess.run(["openvaf", "onepole.va"], cwd=wd, env=ENV,
                       capture_output=True, text=True, timeout=120)
    if c.returncode != 0 or not (wd / "onepole.osdi").exists():
        fail(f"openvaf compile: rc={c.returncode}\n{c.stdout}\n{c.stderr}")
    print("ok   openvaf compiled the corrected idiom -> onepole.osdi")

    s = subprocess.run(["ngspice", "-b", "onepole_va_tb.spice"], cwd=wd, env=ENV,
                       capture_output=True, text=True, timeout=120)
    out = s.stdout + s.stderr
    (wd / "va_sim.log").write_text(out)
    if s.returncode != 0:
        fail(f"ngspice rc={s.returncode}\n{out}")
    if "VA_TB_PASS" not in out or "VA_TB_FAIL" in out:
        fail(f"TB did not pass:\n{out}")
    print("ok   self-checking TB printed VA_TB_PASS")

    # Independent numeric re-check from the meas lines.
    m_dcg = re.search(r"dcg\s*=\s*([-\d.eE+]+)", out)
    m_f3 = re.search(r"f3db\s*=\s*([-\d.eE+]+)", out)
    if not m_dcg or not m_f3:
        fail(f"could not parse meas results from log:\n{out}")
    dcg, f3db = float(m_dcg.group(1)), float(m_f3.group(1))
    print(f"     measured: DC gain = {dcg:.4f} dB, f3db = {f3db/1e6:.4f} MHz")
    if abs(dcg - 20.0) > 0.1:
        fail(f"DC gain {dcg} dB not within 0.1 dB of 20 dB (old idiom gives ~14 dB)")
    if abs(f3db - 1e6) / 1e6 > 0.02:
        fail(f"-3 dB point {f3db} Hz not within 2% of 1 MHz (old idiom gives ~2 MHz)")
    print("ok   DC gain and -3 dB point numerically correct for H(s)=gain/(1+s/wp)")

print("\nVA FIXTURE FLOW PASSED (corrected pole idiom verified end to end)")
