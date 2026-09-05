"""ngspice `.raw` waveform parsing (results sub-window, waveform half).

Only ASCII raw files are supported (`set filetype=ascii` before `write` in
the testbench's `.control` block - see circuit_builder.py's prompt, and the
project's validated DDR-link testbench idiom this mirrors). Binary raw files
and complex-valued (AC) analyses are explicitly NOT supported - both are
detected and reported as a clear error rather than mis-parsed, since ngspice
ascii's per-point line layout (index+tab+value on the first line of a point,
bare tab+value on the rest, blank line between points - see a real sample:
`sed -n '1,20p' <file>.raw`) has no complex-number variant handled here and
binary raw has no plain-text `Values:` section to key off at all.

Size discipline: circuit_builder.py's prompt asks Circuit_Builder to keep
written raw files small (coarse .tran print step, only the handful of
signals actually needed - typically a few hundred KB). This parser adds a
server-side backstop on top of that request, independent of whether the
prompt was followed: a hard byte ceiling on what it will even open, and a
point-count cap on what it returns (evenly-strided downsampling, with the
true point count still reported) so one oversized/misbehaving raw file can't
make a single API response huge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Hard ceiling on-disk size this parser will even attempt to read. Chosen
# well above what circuit_builder.py's prompt asks for (a few hundred KB to
# low single-digit MB for a few thousand points x a handful of signals) but
# far below "hundreds of MB", so a testbench that ignored the point/signal
# guidance fails loudly here instead of the backend hanging on a giant read.
MAX_RAW_BYTES = 25 * 1024 * 1024

# Point-count cap on what a single API response returns. Above this, points
# are evenly strided (not truncated to the head) so the whole time/frequency
# span still shows in the plot, just at lower resolution - the response
# reports both the true point count and how many it actually returned.
MAX_RETURNED_POINTS = 5000


class UnsupportedRawFormat(RuntimeError):
    pass


class RawTooLarge(RuntimeError):
    pass


def _header_int(lines: list[str], prefix: str) -> int | None:
    for line in lines:
        if line.startswith(prefix):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def parse_ascii_raw(path: Path) -> dict[str, Any]:
    """Parse an ngspice ascii .raw file into {signals, sweep_var, points,
    returned_points, downsampled, data: {signal: [values]}}. Raises
    RawTooLarge / UnsupportedRawFormat (both caught by main.py and turned
    into a 4xx with the message verbatim) rather than crashing or silently
    returning garbage."""
    size = path.stat().st_size
    if size > MAX_RAW_BYTES:
        raise RawTooLarge(
            f"{path.name} is {size / 1e6:.1f} MB, over this tool's {MAX_RAW_BYTES / 1e6:.0f} MB "
            "parse limit - the testbench that wrote it should use a coarser .tran step and/or "
            "write fewer signals"
        )
    text = path.read_text(errors="replace")
    lines = text.splitlines()

    nvars = _header_int(lines, "No. Variables")
    npoints = _header_int(lines, "No. Points")
    if nvars is None or npoints is None:
        raise UnsupportedRawFormat(
            f"{path.name} doesn't look like an ngspice raw file (no 'No. Variables:' / "
            "'No. Points:' header found)"
        )

    flags_line = next((l for l in lines if l.startswith("Flags:")), "")
    if "real" not in flags_line:
        kind = flags_line.split(":", 1)[-1].strip() or "unknown"
        raise UnsupportedRawFormat(
            f"{path.name} is a '{kind}' raw file - only real-valued data (transient/DC analyses) "
            "is supported here, not complex-valued data (e.g. an .ac analysis)"
        )

    if "Variables:" not in lines or "Values:" not in lines:
        raise UnsupportedRawFormat(
            f"{path.name} has no plain-text 'Variables:'/'Values:' section - this looks like a "
            "BINARY raw file, which is not supported (only ascii - write it with "
            "`set filetype=ascii` before `write` in the testbench's .control block)"
        )

    vi = lines.index("Variables:")
    names: list[str] = []
    for k in range(nvars):
        parts = lines[vi + 1 + k].split("\t")
        names.append(parts[2] if len(parts) > 2 else parts[-1].strip())

    di = lines.index("Values:")
    # ngspice's ascii layout: each point is `nvars` consecutive non-blank
    # lines (the first prefixed with the point index, e.g. " 12\t<value>";
    # the rest bare "\t<value>"), separated from the next point by a blank
    # line. Filtering blanks and taking every line's LAST tab-separated field
    # sidesteps the index-prefix-on-first-line quirk without needing to
    # parse it.
    body = [l for l in lines[di + 1:] if l.strip()]
    true_points = npoints
    available_points = len(body) // nvars if nvars else 0
    if available_points < true_points:
        # e.g. the writing process was killed mid-.control-block - use what's
        # actually there rather than reading past the end of `body`.
        true_points = available_points

    step = max(1, -(-true_points // MAX_RETURNED_POINTS)) if true_points else 1  # ceil div
    indices = range(0, true_points, step)

    data: dict[str, list[float | None]] = {name: [] for name in names}
    for i in indices:
        base = i * nvars
        for k, name in enumerate(names):
            token = body[base + k].split("\t")[-1]
            try:
                data[name].append(float(token))
            except ValueError:
                data[name].append(None)

    return {
        "signals": names,
        "sweep_var": names[0] if names else None,
        "points": true_points,
        "returned_points": len(indices),
        "downsampled": step > 1,
        "data": data,
    }
