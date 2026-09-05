"""Unit checks for backend/rawfile.py (ngspice ascii .raw parsing).

Plain-python assert style (no pytest on this machine). Run with:
    backend/.venv/bin/python backend/tests/test_rawfile.py

One check actually shells out to real ngspice (free, local, no Circuit_Builder
involved) to generate a genuine ascii .raw file and parse it - not just a
hand-typed fixture string - per this feature's verification requirement.
Skips that one check gracefully if ngspice isn't on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rawfile as rf  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


# A hand-typed but format-faithful ascii raw (2 points, 3 vars: time, v(in),
# v(out)) - exact layout confirmed against a real ngspice-45.2 ascii write
# (leading-tab variable lines, index-prefixed first line per point, blank
# line between points).
SMALL_ASCII_RAW = """Title: * fixture
Date: Sat Sep  5 00:00:00 2026
Command: ngspice-45.2
Plotname: Transient Analysis
Flags: real
No. Variables: 3
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tv(in)\tvoltage
\t2\tv(out)\tvoltage
Values:
 0\t0.000000000000000e+00
\t1.000000000000000e+00
\t0.000000000000000e+00

 1\t1.000000000000000e-09
\t1.000000000000000e+00
\t3.000000000000000e-01

 2\t2.000000000000000e-09
\t1.000000000000000e+00
\t6.000000000000000e-01
"""

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    # --- 1-4: a small well-formed ascii raw --------------------------------
    small = tmp / "small.raw"
    small.write_text(SMALL_ASCII_RAW)
    r = rf.parse_ascii_raw(small)
    ok("signal names parsed in order", r["signals"] == ["time", "v(in)", "v(out)"], r["signals"])
    ok("sweep_var is the first signal", r["sweep_var"] == "time")
    ok("point count matches header", r["points"] == 3, r["points"])
    ok(
        "values parsed correctly (last-tab-field per line)",
        r["data"]["v(out)"] == [0.0, 0.3, 0.6],
        r["data"]["v(out)"],
    )
    ok("not downsampled (well under the cap)", r["downsampled"] is False)

    # --- 5: downsampling above MAX_RETURNED_POINTS -------------------------
    n = rf.MAX_RETURNED_POINTS * 3 + 7
    lines = [
        "Flags: real", f"No. Variables: 2", f"No. Points: {n}",
        "Variables:", "\t0\ttime\ttime", "\t1\tv(x)\tvoltage", "Values:",
    ]
    for i in range(n):
        lines.append(f" {i}\t{i * 1e-9!r}")
        lines.append(f"\t{float(i)!r}")
        lines.append("")
    big = tmp / "big.raw"
    big.write_text("\n".join(lines))
    rbig = rf.parse_ascii_raw(big)
    ok("true point count reported even when downsampled", rbig["points"] == n, rbig["points"])
    ok(
        "returned points capped at MAX_RETURNED_POINTS",
        rbig["returned_points"] <= rf.MAX_RETURNED_POINTS,
        rbig["returned_points"],
    )
    ok("downsampled flag set", rbig["downsampled"] is True)
    ok(
        "downsampling spans the full range (last value near the end, not the head)",
        rbig["data"]["v(x)"][-1] > n * 0.9,
        rbig["data"]["v(x)"][-1],
    )

    # --- 6: oversized file rejected before even reading ---------------------
    huge = tmp / "huge.raw"
    with open(huge, "wb") as f:
        f.seek(rf.MAX_RAW_BYTES + 1024)
        f.write(b"\0")
    try:
        rf.parse_ascii_raw(huge)
        ok("oversized file raises RawTooLarge", False)
    except rf.RawTooLarge:
        ok("oversized file raises RawTooLarge", True)

    # --- 7: binary raw (no Variables:/Values: text section) rejected --------
    binary_like = tmp / "binary.raw"
    binary_like.write_text("Title: x\nFlags: real\nNo. Variables: 2\nNo. Points: 1\nBinary:\n\x00\x01\x02")
    try:
        rf.parse_ascii_raw(binary_like)
        ok("binary raw raises UnsupportedRawFormat", False)
    except rf.UnsupportedRawFormat:
        ok("binary raw raises UnsupportedRawFormat", True)

    # --- 8: complex (AC) raw rejected ----------------------------------------
    complex_like = tmp / "complex.raw"
    complex_like.write_text(
        "Title: x\nFlags: complex\nNo. Variables: 1\nNo. Points: 1\n"
        "Variables:\n\t0\tfrequency\tfrequency\nValues:\n 0\t1.0,0.0\n"
    )
    try:
        rf.parse_ascii_raw(complex_like)
        ok("complex-flagged raw raises UnsupportedRawFormat", False)
    except rf.UnsupportedRawFormat:
        ok("complex-flagged raw raises UnsupportedRawFormat", True)

    # --- 9: garbage (not a raw file at all) ----------------------------------
    garbage = tmp / "garbage.raw"
    garbage.write_text("this is not a raw file\njust some text\n")
    try:
        rf.parse_ascii_raw(garbage)
        ok("non-raw garbage file raises UnsupportedRawFormat", False)
    except rf.UnsupportedRawFormat:
        ok("non-raw garbage file raises UnsupportedRawFormat", True)

    # --- 10: real ngspice-generated raw file (free, local, no Circuit_Builder) --
    ngspice = shutil.which("ngspice")
    if ngspice is None:
        print("skip  [real ngspice raw] ngspice not on PATH")
    else:
        deck = tmp / "rc_tb.spice"
        deck.write_text(
            "* RC step response fixture\n"
            "V1 in 0 PULSE(0 1 1n 0.1n 0.1n 10n 20n)\n"
            "R1 in out 1k\nC1 out 0 1n\n"
            ".tran 10p 40n\n"
            ".control\nset filetype=ascii\nrun\nwrite rc_tb.raw v(in) v(out)\n.endc\n.end\n"
        )
        subprocess.run(
            [ngspice, "-b", "rc_tb.spice"], cwd=tmp, capture_output=True, timeout=60, check=False
        )
        raw_path = tmp / "rc_tb.raw"
        ok("ngspice actually wrote rc_tb.raw", raw_path.is_file())
        rreal = rf.parse_ascii_raw(raw_path)
        ok("real raw: signals include time/v(in)/v(out)", set(rreal["signals"]) == {"time", "v(in)", "v(out)"})
        ok("real raw: v(in) starts at 0 (before the pulse)", rreal["data"]["v(in)"][0] == 0.0)
        ok(
            "real raw: v(out) charges up from 0 over the transient (RC=1us >> 40ns window, "
            "so it stays well under the 1V input step but is clearly rising)",
            0 < rreal["data"]["v(out)"][-1] < 1.0,
            rreal["data"]["v(out)"][-1],
        )

print(f"\nall {CHECKS} checks passed")
