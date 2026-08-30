"""DDR channel models: one Touchstone importer, two emitters (SPICE + RNM).

WHAT THIS IS
------------
A measured/simulated DDR channel arrives as a Touchstone file (.s2p for a
single-ended through path, .s4p for a victim+aggressor pair with crosstalk).
Neither SPICE nor a real-number (RNM) SystemVerilog testbench can consume that
file directly, so this module turns ONE imported file into TWO artifacts that
are, by construction, the same channel:

  1. a vector-fitted, passivity-gated SPICE subcircuit  (channel.sp)
  2. UI-spaced pulse-response cursors                    (cursors.txt/taps.hex)

That shared-source property is the whole point: in the investigation that
proved this flow, the SPICE link measured 46.3% eye closure and the RNM link
45.9% on the same imported channel. If the two emitters ever stop reading the
same fit/network, that agreement is gone.

ARTIFACT CONTRACT (how a selected channel is handed to a Circuit_Builder run)
----------------------------------------------------------------------------
A channel is referenced by its `channel_id`. `artifact_contract(channel_id)`
returns exactly what a run needs; a future run-deck wiring step (NOT done in
this cycle) should consume that dict and nothing else:

    {
      "channel_id":   "<id>",
      "name":         "<user label>",
      "nports":       2 | 4,
      "subckt_path":  "<abs>/channel.sp",   # transistor-level SPICE decks
      "subckt_name":  "ddr_chan" | "ddr_chan4",
      "port_order":   ["p1","p2"] | ["p1","p2","p3","p4"],
      "port_meaning": "p1=TX ... p2=RX ..." ,
      "reference_z":  50.0,
      "cursors_path": "<abs>/cursors.txt",  # RNM / behavioural testbenches
      "taps_hex_path":"<abs>/taps.hex",     # $readmemh, Q16.16
      "ui_s":         312.5e-12,
      "cursor_index_of_main": <int>,        # h0's index in cursors.txt
      "passive":      True,
      "metrics":      {...}
    }

  - A transistor-level deck uses it as:
        .include <subckt_path>
        X1 tx rx <subckt_name>            (2-port; 4-port adds p3 p4)
    with a 50 ohm source and 50 ohm ODT, i.e. exactly the topology of the
    proven testbench. The subckt is S-parameter-based and reference-impedance
    50 ohm; the ports are NOT ideal voltage nodes, they must be driven and
    terminated resistively.
  - An RNM/behavioural deck reads cursors.txt (one float per line, UI-spaced,
    main cursor at cursor_index_of_main) or taps.hex (same values, Q16.16
    fixed point) and convolves it with the symbol sequence.
  - A run must refuse to use a channel whose meta says passive == False.
    Non-passive fits look perfect in-band and blow transients up (see below).

THREE HARD-WON RULES BAKED INTO THIS MODULE (do not "simplify" these)
---------------------------------------------------------------------
1. The fit is gated on ALL-FREQUENCY PASSIVITY, not on rms. The rms-optimal
   fit of the reference channel had +34 dB of gain at 23.6 GHz (max singular
   value 49.9) and drove an ngspice transient to 8064 V while looking perfect
   below 12 GHz. We scan the max singular value over ~1 MHz .. 5 THz and only
   accept < PASSIVITY_LIMIT.
2. scikit-rf's `passivity_enforce()` is NEVER called. On this data it degraded
   rms from 0.004 to 1.25 and still reported the model non-passive.
3. The emitted subcircuit is ALWAYS state-variable rescaled (see
   `rescale_subckt_text`). scikit-rf realizes each pole as C = 1 F with
   R = -1/(p*C), i.e. resistors down to ~1e-11 ohm next to 50 ohm ports;
   ngspice silently clamps R < 1e-12 and the transient matrix goes singular.

Also deliberately NOT offered as channel elements: ngspice's `xfer` code model
(AC-only, and it heap-corrupts on multiport files) and LTRA (no skin-effect or
dielectric loss - it under-predicts dispersion by ~10x).

STORAGE / CACHING
-----------------
Artifacts live under <working_dir>/channels/<channel_id>/ (see
settings.channels_root(), same on-demand-created, never-moved,
previous-locations-stay-readable model as runs/ and libraries/).

`channel_id` IS the cache key: sha256(file bytes) + a hash of the fit
parameters. Re-importing an unchanged file with unchanged parameters resolves
to the same directory, finds a completed meta.json there, and returns it
without refitting - fitting takes minutes, so this matters.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Bumped when the emitted artifacts change in a way that invalidates cached
# fits (it is part of the cache key, so a bump re-fits everything).
ARTIFACT_VERSION = 1

# Passivity gate: max singular value of the fitted S-matrix over the whole
# scanned band must stay below this. 1.02 = 2% tolerance on "no gain", which
# is what separated the stable fits from the 49.9x one (rule 1 above).
PASSIVITY_LIMIT = 1.02

# Band over which passivity is checked - deliberately far beyond the data
# (5 THz), because transient stability is governed by model behaviour OUTSIDE
# the fit band.
PASSIVITY_SCAN_HZ = (1e6, 5e12)
PASSIVITY_SCAN_POINTS = 4000

# Candidate search order: (fit bandwidth in GHz, n_real_poles, n_cmplx_poles).
# This is the grid the investigation swept; the accepted fit for the reference
# 6-inch DDR4 DQ channel came out of it.
FIT_BANDWIDTHS_GHZ = (12, 16, 20)
POLE_GRID = ((1, 8), (1, 12), (2, 16), (2, 20), (2, 24), (2, 30), (3, 36))

DEFAULT_PARAMS: dict[str, Any] = {
    "artifact_version": ARTIFACT_VERSION,
    "passivity_limit": PASSIVITY_LIMIT,
    "fit_bandwidths_ghz": list(FIT_BANDWIDTHS_GHZ),
    "pole_grid": [list(p) for p in POLE_GRID],
    # Pulse-response extraction (rule: the RNM cursors must see the same
    # driver/ODT terminations the SPICE deck does).
    "ui_ps": 312.5,      # DDR4-3200 / 3.2 Gb/s
    "osr": 8,            # samples per UI in the pulse-response FFT
    "rs_ohm": 50.0,      # driver source impedance
    "rl_ohm": 50.0,      # receiver ODT
    "n_pre": 4,          # cursors kept before the main cursor
    "n_post": 20,        # cursors kept after the main cursor
    # Frequencies (Hz) at which insertion loss is reported/compared.
    "il_freqs_hz": [1e8, 1e9, 3.2e9, 6e9],
}

SUPPORTED_SUFFIXES = (".s2p", ".s4p")

ProgressFn = Optional[Callable[[str], None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cache key


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def params_hash(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def channel_id_for(data: bytes, params: dict[str, Any]) -> str:
    """The cache key AND the directory name: file content + fit parameters.
    Same file + same parameters -> same id -> the completed fit is reused."""
    return f"{content_hash(data)[:12]}-{params_hash(params)[:6]}"


def merged_params(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    for key, value in (overrides or {}).items():
        if key in DEFAULT_PARAMS:
            params[key] = value
    params["artifact_version"] = ARTIFACT_VERSION
    return params


# ---------------------------------------------------------------------------
# Rule 3: state-variable rescaling of the scikit-rf subcircuit.

RESCALE_K = 1e-12


def rescale_subckt_text(text: str, k: float = RESCALE_K) -> tuple[str, dict[str, int], tuple[float, float]]:
    """Rescale the vector-fit subcircuit's pole states so ngspice's transient
    engine can actually solve it.

    For a state node x:  C dx/dt + x/R = (inputs) + Gp*x_other
        C -> k*C, R -> R/k  leaves the pole -1/(RC) unchanged and scales
        x -> x/k, so the driving terms (Gx from port voltage, Fx from the port
        branch current) are untouched while the cross-coupling (Gp) and
        readout (Gr) gains must be multiplied by k.
    With k = 1e-12 the state caps become 1 pF and the state resistors land in
    ~10 ohm .. 100 kohm instead of ~1e-11 ohm. Identity-preserving: the
    transfer function, and every pole, is unchanged.

    Returns (rescaled_text, counts, (min_state_R, max_state_R)).
    """
    out: list[str] = []
    stats = {"C": 0, "R": 0, "Gp": 0, "Gr": 0}
    for line in text.splitlines(keepends=True):
        t = line.split()
        if t and t[0].startswith("Cx"):            # state capacitor
            t[3] = repr(float(t[3]) * k)
            stats["C"] += 1
            line = " ".join(t) + "\n"
        elif t and t[0].startswith("Rp"):          # pole resistor
            t[3] = repr(float(t[3]) / k)
            stats["R"] += 1
            line = " ".join(t) + "\n"
        elif t and t[0].startswith("Gp"):          # re/im cross-coupling
            t[5] = repr(float(t[5]) * k)
            stats["Gp"] += 1
            line = " ".join(t) + "\n"
        elif t and t[0].startswith("Gr"):          # state readout
            t[5] = repr(float(t[5]) * k)
            stats["Gr"] += 1
            line = " ".join(t) + "\n"
        out.append(line)
    text_out = "".join(out)
    vals = [float(l.split()[3]) for l in out if l.split() and l.split()[0].startswith("Rp")]
    span = (min(vals), max(vals)) if vals else (0.0, 0.0)
    return text_out, stats, span


# ---------------------------------------------------------------------------
# Rule 1: the passivity gate.


def max_singular_value(vf, nports: int, fmin: float, fmax: float, n: int = PASSIVITY_SCAN_POINTS):
    """Largest singular value of the FITTED S-matrix over [fmin, fmax], and
    the frequency where it occurs. > 1 means the model has gain there."""
    import numpy as np

    f = np.logspace(np.log10(fmin), np.log10(fmax), n)
    S = np.zeros((n, nports, nports), complex)
    for i in range(nports):
        for j in range(nports):
            S[:, i, j] = vf.get_model_response(i, j, freqs=f)
    sv = np.linalg.svd(S, compute_uv=False).max(axis=1)
    k = int(sv.argmax())
    return float(sv[k]), float(f[k])


def evaluate_fit(vf, nports: int, passivity_limit: float = PASSIVITY_LIMIT) -> dict[str, Any]:
    """Stability + all-frequency passivity verdict for one candidate fit."""
    import numpy as np

    poles = np.asarray(vf.poles)
    pole_max = float(np.abs(poles).max())
    pole_real_max = float(poles.real.max())
    sv_all, f_all = max_singular_value(vf, nports, *PASSIVITY_SCAN_HZ)
    stable = pole_real_max < 0 and pole_max < 1e13
    passive = sv_all < passivity_limit
    if not stable:
        verdict = "reject:unstable"
    elif not passive:
        verdict = f"reject:gain {sv_all:.1f}x @ {f_all / 1e9:.1f} GHz"
    else:
        verdict = "accept"
    return {
        "stable": stable,
        "passive": passive,
        "accepted": bool(stable and passive),
        "max_singular_value": sv_all,
        "max_singular_value_freq_hz": f_all,
        "pole_real_max": pole_real_max,
        "pole_abs_max": pole_max,
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Emitter 2: UI-spaced pulse-response cursors (the RNM side).


def extract_cursors(ntw, params: dict[str, Any]) -> dict[str, Any]:
    """UI-spaced pulse-response cursors for the port1 -> port2 through path.

    The channel is terminated in its real driver impedance (rs_ohm) and ODT
    (rl_ohm) first, via the Z-parameters, so the pulse response carries the
    reflection behaviour the RNM will actually see. Same quantity IBIS-AMI
    calls the channel pulse response.
    """
    import numpy as np

    ui = float(params["ui_ps"]) * 1e-12
    osr = int(params["osr"])
    rs = float(params["rs_ohm"])
    rl = float(params["rl_ohm"])

    two_port = ntw if ntw.nports == 2 else ntw.subnetwork([0, 1])
    f = two_port.f
    Z = two_port.z
    z11, z12, z21, z22 = Z[:, 0, 0], Z[:, 0, 1], Z[:, 1, 0], Z[:, 1, 1]
    H = (z21 * rl) / ((z11 + rs) * (z22 + rl) - z12 * z21)

    fs = osr / ui
    n = 8192
    fg = np.arange(n // 2 + 1) * fs / n
    Hg = np.interp(fg, f, H.real) + 1j * np.interp(fg, f, H.imag)
    Hg[fg > f.max()] = 0.0                      # never extrapolate above the data
    h = np.fft.irfft(Hg, n=n) * fs              # impulse response
    rect = np.ones(osr) / fs                    # one-UI-wide transmitted pulse
    p = np.convolve(h, rect)[:n]
    t = np.arange(n) / fs

    pk = int(p.argmax())
    cur = p[pk % osr:: osr]                     # sample at the peak's phase
    main = int(cur.argmax())
    n_pre = int(params["n_pre"])
    n_post = int(params["n_post"])
    lo = max(0, main - n_pre)
    hi = min(len(cur), main + n_post + 1)
    kept = cur[lo:hi]
    main_idx = main - lo

    isi = float(np.sum(np.abs(cur)) - abs(cur[main]))
    h0 = float(cur[main])
    return {
        "cursors": [float(v) for v in kept],
        "index_of_main": main_idx,
        "h0": h0,
        "precursors": [float(v) for v in kept[:main_idx]],
        "postcursors": [float(v) for v in kept[main_idx + 1:]],
        "sum_abs_isi": isi,
        "worst_case_eye": float(2 * (h0 - isi)),
        "peak_time_ps": float(t[pk] * 1e12),
        "peak_time_ui": float(t[pk] / ui),
        "ui_ps": float(params["ui_ps"]),
        "osr": osr,
    }


def cursors_to_hex(cursors: list[float]) -> str:
    """Q16.16 fixed point, one word per line - $readmemh-ready."""
    return "".join(f"{int(round(v * 65536)) & 0xFFFFFFFF:08x}\n" for v in cursors)


# ---------------------------------------------------------------------------
# Metrics


def _db(x):
    import numpy as np

    return float(20 * np.log10(abs(x)))


def insertion_loss_table(ntw, vf, freqs: list[float]) -> list[dict[str, float]]:
    """Source-data vs fitted-model S21 (and, for a 4-port, NEXT S31 / FEXT
    S41) at the requested frequencies - the check that the emitted model is
    still the channel that was imported."""
    import numpy as np

    rows = []
    for ft in freqs:
        if ft > ntw.f.max():
            continue
        i = int(np.argmin(abs(ntw.f - ft)))
        fa = np.array([ft])
        row = {
            "freq_hz": float(ft),
            "data_db": _db(ntw.s[i, 1, 0]),
            "fit_db": _db(vf.get_model_response(1, 0, freqs=fa)[0]),
            "return_loss_data_db": _db(ntw.s[i, 0, 0]),
            "return_loss_fit_db": _db(vf.get_model_response(0, 0, freqs=fa)[0]),
        }
        row["error_db"] = row["fit_db"] - row["data_db"]
        if ntw.nports == 4:
            row["next_data_db"] = _db(ntw.s[i, 2, 0])
            row["next_fit_db"] = _db(vf.get_model_response(2, 0, freqs=fa)[0])
            row["fext_data_db"] = _db(ntw.s[i, 3, 0])
            row["fext_fit_db"] = _db(vf.get_model_response(3, 0, freqs=fa)[0])
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# The fit search


def _fit_one(ntw, fmax_ghz: int, n_real: int, n_cmplx: int):
    """One vector-fit candidate. Returns (vf, rms) or None if it blew up."""
    import skrf as rf  # noqa: F401  (Network slicing below needs skrf loaded)
    from skrf.vectorFitting import VectorFitting

    band = ntw[f"1-{fmax_ghz * 1000}mhz"]
    if band.f.size < 10:
        return None
    vf = VectorFitting(band)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vf.vector_fit(
                n_poles_real=n_real,
                n_poles_cmplx=n_cmplx,
                fit_constant=True,
                fit_proportional=False,
            )
    except Exception:
        return None
    return vf, float(vf.get_rms_error())


def search_passive_fit(ntw, params: dict[str, Any], progress: ProgressFn = None) -> dict[str, Any]:
    """Sweep (fit bandwidth x pole count) and return the LOWEST-RMS candidate
    that passes the stability + all-frequency passivity gate.

    Gating on the gate, never on rms: an rms-optimal but non-passive fit is
    rejected outright, no matter how good it looks in band. Raises
    ChannelFitError if nothing in the grid passes.
    """
    limit = float(params.get("passivity_limit", PASSIVITY_LIMIT))
    trials: list[dict[str, Any]] = []
    accepted: list[tuple[float, Any, dict[str, Any]]] = []
    nports = ntw.nports

    for fmax_ghz in params["fit_bandwidths_ghz"]:
        for n_real, n_cmplx in params["pole_grid"]:
            label = f"{n_real}r/{n_cmplx}c fit<={fmax_ghz}GHz"
            if progress:
                progress(f"fitting {label}")
            got = _fit_one(ntw, int(fmax_ghz), int(n_real), int(n_cmplx))
            if got is None:
                trials.append({"bandwidth_ghz": fmax_ghz, "n_real": n_real,
                               "n_cmplx": n_cmplx, "verdict": "reject:fit failed"})
                continue
            vf, rms = got
            verdict = evaluate_fit(vf, nports, limit)
            trials.append({
                "bandwidth_ghz": fmax_ghz, "n_real": n_real, "n_cmplx": n_cmplx,
                "rms": rms, **{k: verdict[k] for k in
                               ("max_singular_value", "max_singular_value_freq_hz", "verdict")},
            })
            if progress:
                progress(
                    f"{label}: rms={rms:.5f} max_sv={verdict['max_singular_value']:.3f} "
                    f"-> {verdict['verdict']}"
                )
            if verdict["accepted"]:
                accepted.append((rms, vf, {**verdict, "bandwidth_ghz": fmax_ghz,
                                           "n_real": n_real, "n_cmplx": n_cmplx, "rms": rms}))

    if not accepted:
        worst = min((t.get("max_singular_value", float("inf")) for t in trials), default=float("inf"))
        raise ChannelFitError(
            "no passive fit found: every candidate in the search grid was rejected "
            f"(best all-frequency max singular value {worst:.3f}, limit {limit}). "
            "Widen the pole grid / fit bandwidth, or check the Touchstone data."
        )
    accepted.sort(key=lambda a: a[0])
    rms, vf, info = accepted[0]
    return {"vf": vf, "info": info, "trials": trials}


class ChannelFitError(RuntimeError):
    """The importer could not produce a usable (passive, stable) model."""


# ---------------------------------------------------------------------------
# Import: file -> artifacts


def subckt_name_for(nports: int) -> str:
    # Matches the proven testbenches (chan/s4p_tb.cir etc.) so an emitted
    # subckt drops straight into them.
    return "ddr_chan" if nports == 2 else f"ddr_chan{nports}"


def read_network(path: Path):
    import skrf as rf

    return rf.Network(str(path))


def channel_dir(channel_id: str, root: Optional[Path] = None) -> Path:
    if root is None:
        from settings import channels_root

        root = channels_root()
    return Path(root) / channel_id


def read_meta(chan_dir: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads((chan_dir / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_meta(chan_dir: Path, meta: dict[str, Any]) -> None:
    tmp = chan_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, default=str))
    tmp.replace(chan_dir / "meta.json")


def prepare_channel(
    data: bytes,
    filename: str,
    name: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> tuple[str, Path, dict[str, Any], bool]:
    """Resolve a Touchstone upload to its cache slot.

    Returns (channel_id, chan_dir, meta, cached). `cached` is True when a
    COMPLETED fit for this exact file+parameters already exists on disk - the
    caller must then not refit (that is the whole point: fits take minutes).
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported channel file '{filename}': expected one of "
            f"{', '.join(SUPPORTED_SUFFIXES)}"
        )
    par = merged_params(params)
    cid = channel_id_for(data, par)
    cdir = channel_dir(cid, root)
    existing = read_meta(cdir) if cdir.is_dir() else None
    if existing and existing.get("state") == "done" and existing.get("status") == "success":
        return cid, cdir, existing, True

    cdir.mkdir(parents=True, exist_ok=True)
    source_name = f"source{suffix}"
    (cdir / source_name).write_bytes(data)
    meta = {
        "channel_id": cid,
        "name": name or Path(filename).stem,
        "filename": filename,
        "source_file": source_name,
        "nports": 4 if suffix == ".s4p" else 2,
        "content_sha256": content_hash(data),
        "fit_params": par,
        "state": "running",
        "status": None,
        "reason": None,
        "progress": "queued",
        "created_at": _now(),
        "finished_at": None,
        "metrics": None,
        "artifacts": {},
    }
    write_meta(cdir, meta)
    return cid, cdir, meta, False


def run_fit(chan_dir: Path, progress: ProgressFn = None) -> dict[str, Any]:
    """Do the expensive work for a prepared channel directory: search for a
    passive fit, emit the rescaled SPICE subckt and the RNM cursors, and
    record metrics in meta.json. Returns the updated meta."""
    meta = read_meta(chan_dir)
    if meta is None:
        raise ChannelFitError(f"no meta.json in {chan_dir}")
    params = meta["fit_params"]
    log_lines: list[str] = []

    def note(msg: str) -> None:
        log_lines.append(msg)
        meta["progress"] = msg
        write_meta(chan_dir, meta)
        (chan_dir / "fit.log").write_text("\n".join(log_lines) + "\n")
        if progress:
            progress(msg)

    try:
        note(f"reading {meta['filename']}")
        ntw = read_network(chan_dir / meta["source_file"])
        if ntw.nports not in (2, 4):
            raise ChannelFitError(f"unsupported port count {ntw.nports} (expected 2 or 4)")
        meta["nports"] = ntw.nports
        note(f"{ntw.nports}-port network, {ntw.f.size} frequency points, "
             f"{ntw.f.min() / 1e6:.1f} MHz .. {ntw.f.max() / 1e9:.2f} GHz")

        found = search_passive_fit(ntw, params, progress=note)
        vf, info, trials = found["vf"], found["info"], found["trials"]
        note(f"accepted {info['n_real']}r/{info['n_cmplx']}c fit<={info['bandwidth_ghz']}GHz "
             f"rms={info['rms']:.5f} max_sv={info['max_singular_value']:.4f}")

        # Emitter 1: SPICE subckt (always rescaled - rule 3).
        raw_sp = chan_dir / "channel_raw.sp"
        sub_name = subckt_name_for(ntw.nports)
        vf.write_spice_subcircuit_s(str(raw_sp), fitted_model_name=sub_name)
        text, stats, (rmin, rmax) = rescale_subckt_text(raw_sp.read_text())
        (chan_dir / "channel.sp").write_text(text)
        raw_sp.unlink(missing_ok=True)
        note(f"emitted channel.sp ({stats['C']} states, state R {rmin:.3g}..{rmax:.3g} ohm)")

        # Emitter 2: UI-spaced cursors, from the SAME imported network.
        cur = extract_cursors(ntw, params)
        (chan_dir / "cursors.txt").write_text(
            "".join(f"{v:.8f}\n" for v in cur["cursors"])
        )
        (chan_dir / "taps.hex").write_text(cursors_to_hex(cur["cursors"]))
        note(f"emitted cursors.txt ({len(cur['cursors'])} taps, h0={cur['h0']:.5f}, "
             f"sum|ISI|={cur['sum_abs_isi']:.5f})")

        il = insertion_loss_table(ntw, vf, [float(x) for x in params["il_freqs_hz"]])
        meta["metrics"] = {
            "nports": ntw.nports,
            "n_freq_points": int(ntw.f.size),
            "freq_min_hz": float(ntw.f.min()),
            "freq_max_hz": float(ntw.f.max()),
            "reference_z": float(ntw.z0[0, 0].real),
            "subckt_name": sub_name,
            # The passivity verdict - the number that decides whether this
            # model may be used in a transient at all.
            "passive": True,
            "max_singular_value": info["max_singular_value"],
            "max_singular_value_freq_hz": info["max_singular_value_freq_hz"],
            "passivity_limit": float(params.get("passivity_limit", PASSIVITY_LIMIT)),
            "rms_error": info["rms"],
            "n_poles_real": info["n_real"],
            "n_poles_cmplx": info["n_cmplx"],
            "n_poles_total": info["n_real"] + 2 * info["n_cmplx"],
            "fit_bandwidth_hz": float(info["bandwidth_ghz"]) * 1e9,
            "state_resistor_min_ohm": rmin,
            "state_resistor_max_ohm": rmax,
            "rescale_counts": stats,
            "insertion_loss": il,
            "cursors": cur,
            "trials": trials,
        }
        meta["artifacts"] = {
            "subckt": "channel.sp",
            "cursors": "cursors.txt",
            "taps_hex": "taps.hex",
            "source": meta["source_file"],
            "log": "fit.log",
        }
        meta.update(state="done", status="success", reason=None,
                    progress="done", finished_at=_now())
    except Exception as exc:  # noqa: BLE001 - the real failure mode; surface it
        meta.update(state="done", status="failed", reason=f"{type(exc).__name__}: {exc}",
                    finished_at=_now())
        log_lines.append(f"FAILED: {meta['reason']}")
        (chan_dir / "fit.log").write_text("\n".join(log_lines) + "\n")

    write_meta(chan_dir, meta)
    return meta


def channel_import(
    path: str | Path,
    name: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    root: Optional[Path] = None,
    progress: ProgressFn = None,
) -> dict[str, Any]:
    """Import a Touchstone file end to end (blocking).

    Returns {"channel_id", "spice_subckt_path", "cursors_path", "metrics",
    "meta", "cached"}. On a cache hit nothing is refitted.
    """
    path = Path(path)
    data = path.read_bytes()
    cid, cdir, meta, cached = prepare_channel(data, path.name, name, params, root)
    if not cached:
        meta = run_fit(cdir, progress=progress)
    if meta.get("status") != "success":
        raise ChannelFitError(meta.get("reason") or "channel fit failed")
    return {
        "channel_id": cid,
        "spice_subckt_path": str(cdir / meta["artifacts"]["subckt"]),
        "cursors_path": str(cdir / meta["artifacts"]["cursors"]),
        "metrics": meta["metrics"],
        "meta": meta,
        "cached": cached,
    }


def artifact_contract(meta: dict[str, Any], chan_dir: Path) -> dict[str, Any]:
    """The handoff payload for a future Circuit_Builder run - see the module
    docstring. Deliberately small and absolute-pathed: a run deck should need
    nothing else about the channel."""
    metrics = meta.get("metrics") or {}
    cur = metrics.get("cursors") or {}
    nports = int(meta.get("nports") or 2)
    return {
        "channel_id": meta.get("channel_id"),
        "name": meta.get("name"),
        "nports": nports,
        "subckt_path": str(chan_dir / (meta.get("artifacts", {}).get("subckt") or "channel.sp")),
        "subckt_name": metrics.get("subckt_name") or subckt_name_for(nports),
        "port_order": [f"p{i + 1}" for i in range(nports)],
        "port_meaning": (
            "p1 = TX/driver end, p2 = RX/ODT end"
            if nports == 2
            else "p1 = victim TX, p2 = victim RX, p3 = aggressor near end (NEXT), "
                 "p4 = aggressor far end (FEXT)"
        ),
        "reference_z": metrics.get("reference_z", 50.0),
        "cursors_path": str(chan_dir / (meta.get("artifacts", {}).get("cursors") or "cursors.txt")),
        "taps_hex_path": str(chan_dir / (meta.get("artifacts", {}).get("taps_hex") or "taps.hex")),
        "ui_s": float(cur.get("ui_ps", DEFAULT_PARAMS["ui_ps"])) * 1e-12,
        "cursor_index_of_main": cur.get("index_of_main"),
        "passive": bool(metrics.get("passive")),
        "metrics": {
            k: metrics.get(k)
            for k in ("max_singular_value", "rms_error", "n_poles_total", "insertion_loss")
        },
    }


def list_channels(roots: Optional[list[Path]] = None) -> list[dict[str, Any]]:
    """Summary of every imported channel across all channel roots (current
    working dir first, then previous ones - same model as runs)."""
    if roots is None:
        from settings import all_channels_roots

        roots = all_channels_roots()
    seen: dict[str, dict[str, Any]] = {}
    for root in roots:
        for meta_path in sorted(Path(root).glob("*/meta.json")):
            meta = read_meta(meta_path.parent)
            if not meta:
                continue
            cid = meta.get("channel_id") or meta_path.parent.name
            if cid in seen:
                continue  # current root wins
            metrics = meta.get("metrics") or {}
            seen[cid] = {
                "channel_id": cid,
                "name": meta.get("name"),
                "filename": meta.get("filename"),
                "nports": meta.get("nports"),
                "state": meta.get("state"),
                "status": meta.get("status"),
                "reason": meta.get("reason"),
                "progress": meta.get("progress"),
                "created_at": meta.get("created_at"),
                "finished_at": meta.get("finished_at"),
                "passive": metrics.get("passive"),
                "max_singular_value": metrics.get("max_singular_value"),
                "rms_error": metrics.get("rms_error"),
                "h0": (metrics.get("cursors") or {}).get("h0"),
            }
    return sorted(seen.values(), key=lambda c: c.get("created_at") or "", reverse=True)


def find_channel_dir(channel_id: str, roots: Optional[list[Path]] = None) -> Optional[Path]:
    if roots is None:
        from settings import all_channels_roots

        roots = all_channels_roots()
    for root in roots:
        candidate = Path(root) / channel_id
        if candidate.is_dir():
            return candidate
    return None


def import_from_local_path(src: str) -> bytes:
    """Server-side path attach (same convention as spec_docs: backend and
    browser share this machine). Raises ValueError with a user-facing message."""
    path = Path(src).expanduser()
    if not path.is_absolute():
        raise ValueError(f"channel path must be absolute; got {src!r}")
    if not path.is_file():
        raise ValueError(f"no such file: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(
            f"unsupported channel file '{path.name}': expected one of "
            f"{', '.join(SUPPORTED_SUFFIXES)}"
        )
    return path.read_bytes()


__all__ = [
    "ARTIFACT_VERSION",
    "ChannelFitError",
    "artifact_contract",
    "channel_dir",
    "channel_id_for",
    "channel_import",
    "extract_cursors",
    "find_channel_dir",
    "import_from_local_path",
    "list_channels",
    "prepare_channel",
    "read_meta",
    "rescale_subckt_text",
    "run_fit",
    "search_passive_fit",
]