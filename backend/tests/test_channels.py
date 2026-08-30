"""Tests for the DDR channel-model importer (backend/channels.py) and its API.

No claude invocations, no paid runs - this suite only does vector fitting on
two small Touchstone fixtures (tests/fixtures/chan_2port.s2p, chan_4port.s4p,
decimated from the reference DDR4 channels the flow was proven on).

The single most important test here is `test_rms_optimal_but_active_fit_is_rejected`:
the historical failure was an rms-optimal fit (rms 0.0038) with +34 dB of gain
at 23.6 GHz that blew an ngspice transient to 8064 V while looking perfect
in-band. The importer must reject that fit no matter how good its rms is.

Run:  cd backend && .venv/bin/python -m pytest tests/test_channels.py -q
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import channels as ch

FIXTURES = Path(__file__).resolve().parent / "fixtures"
S2P = FIXTURES / "chan_2port.s2p"
S4P = FIXTURES / "chan_4port.s4p"

# Fit parameters used by the tests: the same search machinery, narrowed to one
# fit bandwidth so a test fit takes seconds instead of half a minute.
FAST_PARAMS = {"fit_bandwidths_ghz": [16]}
FAST_PARAMS_4P = {"fit_bandwidths_ghz": [12], "pole_grid": [[1, 8], [1, 12], [2, 16]]}


# ---------------------------------------------------------------------------
# Rule 1: the passivity gate


class _StubVF:
    """A stand-in vector-fit result with a prescribed max singular value.

    Diagonal S with magnitude `inband` below 20 GHz and `gain` above it, so
    the all-frequency scan sees exactly `gain` while an in-band-only check
    would see nothing wrong - the shape of the real failure.
    """

    def __init__(self, gain: float, unstable: bool = False):
        self.gain = gain
        self.poles = np.array([1e9 + 0j] if unstable else [-1e9 + 0j])

    def get_model_response(self, i, j, freqs=None):
        f = np.asarray(freqs, dtype=float)
        if i != j:
            return np.zeros_like(f, dtype=complex)
        return np.where(f >= 20e9, self.gain, 0.5).astype(complex)


def test_evaluate_fit_rejects_the_historical_active_fit():
    """The 49.9x-gain fit (rms-optimal, in-band-perfect) must be rejected."""
    verdict = ch.evaluate_fit(_StubVF(49.9), nports=2)
    assert verdict["accepted"] is False
    assert verdict["passive"] is False
    assert verdict["max_singular_value"] == pytest.approx(49.9)
    assert verdict["max_singular_value_freq_hz"] >= 20e9
    assert "gain" in verdict["verdict"]


def test_evaluate_fit_accepts_a_passive_stable_fit():
    verdict = ch.evaluate_fit(_StubVF(0.98), nports=2)
    assert verdict["accepted"] is True and verdict["passive"] and verdict["stable"]


def test_evaluate_fit_rejects_right_half_plane_poles():
    verdict = ch.evaluate_fit(_StubVF(0.5, unstable=True), nports=2)
    assert verdict["accepted"] is False
    assert verdict["verdict"] == "reject:unstable"


def test_rms_optimal_but_active_fit_is_rejected(monkeypatch):
    """THE test: given a candidate with far better rms but 49.9x gain and a
    modest-rms passive candidate, the search must return the PASSIVE one.
    Gating on rms (as the first version of this flow did) picks the bad one.
    """
    bad, good = _StubVF(49.9), _StubVF(0.98)
    plan = {(1, 8): (bad, 0.0038), (2, 16): (good, 0.0500)}

    def fake_fit(ntw, fmax_ghz, n_real, n_cmplx):
        return plan[(n_real, n_cmplx)]

    monkeypatch.setattr(ch, "_fit_one", fake_fit)
    ntw = ch.read_network(S2P)
    params = ch.merged_params({"fit_bandwidths_ghz": [16], "pole_grid": [[1, 8], [2, 16]]})
    found = ch.search_passive_fit(ntw, params)

    assert found["vf"] is good, "the passive fit must win over the lower-rms active one"
    assert found["info"]["rms"] == pytest.approx(0.05)
    rejected = [t for t in found["trials"] if t["verdict"].startswith("reject")]
    assert len(rejected) == 1 and rejected[0]["rms"] == pytest.approx(0.0038)
    assert rejected[0]["max_singular_value"] == pytest.approx(49.9)


def test_search_raises_when_nothing_passes_the_gate():
    """No silent fallback to 'best rms anyway' - an unmeetable gate fails."""
    ntw = ch.read_network(S2P)
    params = ch.merged_params(
        {"fit_bandwidths_ghz": [12], "pole_grid": [[1, 8]], "passivity_limit": 0.10}
    )
    with pytest.raises(ch.ChannelFitError) as exc:
        ch.search_passive_fit(ntw, params)
    assert "no passive fit" in str(exc.value)


def test_max_singular_value_scans_far_above_the_data():
    """Transient stability is set outside the fit band, so the scan must go
    to ~5 THz, not stop at the data's top frequency."""
    assert ch.PASSIVITY_SCAN_HZ[1] >= 1e12
    sv, f = ch.max_singular_value(_StubVF(3.0), 2, 1e6, 5e12, n=200)
    assert sv == pytest.approx(3.0) and f >= 20e9


# ---------------------------------------------------------------------------
# Rule 3: state-variable rescaling


RAW_SUBCKT = """.SUBCKT ddr_chan p1 p2
V1 p1 s1 0
R1 s1 0 50.0
Gr1_re_1_1 0 s1 x1_re_a1 0 0.0027933810634037126
Gp1_a1 0 x1_re_a1 x1_im_a1 0 1.5e11
Cx1_re_a1 x1_re_a1 0 1.0
Gx1_re_a1 0 x1_re_a1 p1 0 0.07071067811865475
Fx1_re_a1 0 x1_re_a1 V1 3.5355339059327378
Rp1_re_a1 0 x1_re_a1 1.1e-11
.ENDS ddr_chan
"""


def test_rescaling_produces_sane_resistor_values():
    text, stats, (rmin, rmax) = ch.rescale_subckt_text(RAW_SUBCKT)
    lines = {l.split()[0]: l.split() for l in text.splitlines() if l.split()}
    assert float(lines["Cx1_re_a1"][3]) == pytest.approx(1e-12)      # 1 F -> 1 pF
    assert float(lines["Rp1_re_a1"][3]) == pytest.approx(11.0)       # 1.1e-11 -> 11 ohm
    # ngspice silently clamps R < 1e-12; nothing may land anywhere near it.
    assert rmin > 1e-3 and rmin == pytest.approx(11.0)
    assert stats == {"C": 1, "R": 1, "Gp": 1, "Gr": 1}


def test_rescaling_is_identity_preserving():
    """R*C (i.e. the pole) and the driving gains must be untouched; only the
    cross-coupling/readout gains scale, by exactly k."""
    text, _, _ = ch.rescale_subckt_text(RAW_SUBCKT)
    lines = {l.split()[0]: l.split() for l in text.splitlines() if l.split()}
    r_new, c_new = float(lines["Rp1_re_a1"][3]), float(lines["Cx1_re_a1"][3])
    assert r_new * c_new == pytest.approx(1.1e-11 * 1.0)             # pole unchanged
    assert float(lines["Gx1_re_a1"][5]) == pytest.approx(0.07071067811865475)  # input gain
    assert float(lines["Fx1_re_a1"][4]) == pytest.approx(3.5355339059327378)
    assert float(lines["Gr1_re_1_1"][5]) == pytest.approx(0.0027933810634037126e-12)
    assert float(lines["Gp1_a1"][5]) == pytest.approx(1.5e11 * 1e-12)


def test_passivity_enforce_is_never_called():
    """Rule 2: scikit-rf's passivity_enforce degraded rms 0.004 -> 1.25 and
    still reported the model non-passive. It must not appear in the module."""
    src = (Path(__file__).resolve().parent.parent / "channels.py").read_text()
    # No call site anywhere (the module docstring mentions the name only to
    # say why it is banned - that mention has no leading dot/receiver).
    assert ".passivity_enforce(" not in src
    assert "vf.passivity_enforce" not in src


# ---------------------------------------------------------------------------
# End-to-end import: both port counts


@pytest.fixture(scope="module")
def imported_s2p(tmp_path_factory):
    root = tmp_path_factory.mktemp("channels2p")
    return ch.channel_import(S2P, name="fixture 2-port", params=FAST_PARAMS, root=root), root


@pytest.fixture(scope="module")
def imported_s4p(tmp_path_factory):
    root = tmp_path_factory.mktemp("channels4p")
    return ch.channel_import(S4P, name="fixture 4-port", params=FAST_PARAMS_4P, root=root), root


def test_s2p_import_emits_both_artifacts(imported_s2p):
    res, _ = imported_s2p
    m = res["metrics"]
    assert m["nports"] == 2 and m["subckt_name"] == "ddr_chan"
    assert m["passive"] is True and m["max_singular_value"] < ch.PASSIVITY_LIMIT
    assert 0 < m["rms_error"] < 0.05
    assert m["n_poles_total"] == m["n_poles_real"] + 2 * m["n_poles_cmplx"]
    sp = Path(res["spice_subckt_path"])
    cur = Path(res["cursors_path"])
    assert sp.is_file() and cur.is_file()
    assert ".SUBCKT ddr_chan p1 p2" in sp.read_text()
    # Emitted subckt carries the rescaling (rule 3), not 1 F / 1e-11 ohm states.
    assert m["state_resistor_min_ohm"] > 1.0
    caps = [float(l.split()[3]) for l in sp.read_text().splitlines()
            if l.split() and l.split()[0].startswith("Cx")]
    assert caps and all(c == pytest.approx(1e-12) for c in caps)


def test_s2p_insertion_loss_tracks_the_source_data(imported_s2p):
    res, _ = imported_s2p
    for row in res["metrics"]["insertion_loss"]:
        assert abs(row["error_db"]) < 0.1, f"fit drifted at {row['freq_hz']}: {row}"


def test_s4p_import_includes_crosstalk(imported_s4p):
    res, _ = imported_s4p
    m = res["metrics"]
    assert m["nports"] == 4 and m["subckt_name"] == "ddr_chan4"
    assert m["passive"] is True
    sp = Path(res["spice_subckt_path"]).read_text()
    assert ".SUBCKT ddr_chan4 p1 p2 p3 p4" in sp
    row = m["insertion_loss"][1]
    # Crosstalk must be reported and be far below the through path.
    assert row["next_data_db"] < row["data_db"] - 20
    assert abs(row["next_fit_db"] - row["next_data_db"]) < 1.0
    # FEXT sits ~60 dB down on this channel, i.e. at the fit's noise floor -
    # what matters is that it is still reported and still negligible, not
    # that a -60 dB term is matched to a fraction of a dB.
    assert row["fext_fit_db"] < row["data_db"] - 40


def test_unsupported_suffix_is_refused(tmp_path):
    bad = tmp_path / "channel.s3p"
    bad.write_text("! nope\n")
    with pytest.raises(ValueError):
        ch.prepare_channel(b"! nope\n", bad.name, root=tmp_path)


# ---------------------------------------------------------------------------
# Cursors (the RNM emitter)


def test_cursors_extracted_correctly(imported_s2p):
    res, _ = imported_s2p
    cur = res["metrics"]["cursors"]
    taps = cur["cursors"]
    assert len(taps) == len(cur["precursors"]) + 1 + len(cur["postcursors"])
    assert taps[cur["index_of_main"]] == pytest.approx(cur["h0"])
    assert cur["h0"] == pytest.approx(max(taps))          # main cursor is the peak
    assert 0.2 < cur["h0"] < 0.5                          # ~half swing, minus loss
    assert cur["sum_abs_isi"] > 0
    assert cur["worst_case_eye"] == pytest.approx(2 * (cur["h0"] - cur["sum_abs_isi"]))
    assert cur["ui_ps"] == 312.5 and cur["osr"] == 8


def test_cursor_files_match_the_metrics(imported_s2p):
    res, root = imported_s2p
    cdir = Path(res["spice_subckt_path"]).parent
    taps = res["metrics"]["cursors"]["cursors"]
    on_disk = [float(x) for x in (cdir / "cursors.txt").read_text().split()]
    assert on_disk == pytest.approx(taps, abs=1e-8)
    hexed = (cdir / "taps.hex").read_text().split()
    assert len(hexed) == len(taps)
    # Q16.16 round-trip of the main cursor.
    main = res["metrics"]["cursors"]["index_of_main"]
    assert int(hexed[main], 16) / 65536 == pytest.approx(taps[main], abs=2e-5)


def test_cursors_follow_the_ui_setting(tmp_path):
    """The cursor emitter is parameterised by the UI - a different data rate
    gives a different tap spacing, and (being in the cache key) its own slot."""
    ntw = ch.read_network(S2P)
    slow = ch.extract_cursors(ntw, ch.merged_params({"ui_ps": 625.0}))
    fast = ch.extract_cursors(ntw, ch.merged_params({"ui_ps": 312.5}))
    assert slow["ui_ps"] == 625.0 and fast["ui_ps"] == 312.5
    # Twice the UI -> less ISI leaking into the neighbouring symbols.
    assert slow["sum_abs_isi"] < fast["sum_abs_isi"]


# ---------------------------------------------------------------------------
# Caching


def test_cache_hit_avoids_refitting(tmp_path, monkeypatch):
    first = ch.channel_import(S2P, params=FAST_PARAMS, root=tmp_path)
    assert first["cached"] is False

    def explode(*a, **k):
        raise AssertionError("run_fit called again for an unchanged file")

    monkeypatch.setattr(ch, "run_fit", explode)
    second = ch.channel_import(S2P, params=FAST_PARAMS, root=tmp_path)
    assert second["cached"] is True
    assert second["channel_id"] == first["channel_id"]
    assert second["metrics"]["rms_error"] == first["metrics"]["rms_error"]


def test_cache_key_covers_file_content_and_fit_params(tmp_path):
    data = S2P.read_bytes()
    p1 = ch.merged_params(FAST_PARAMS)
    p2 = ch.merged_params({**FAST_PARAMS, "ui_ps": 625.0})
    assert ch.channel_id_for(data, p1) != ch.channel_id_for(data, p2)
    assert ch.channel_id_for(data + b"\n", p1) != ch.channel_id_for(data, p1)
    assert ch.channel_id_for(data, p1) == ch.channel_id_for(data, dict(p1))


def test_failed_fit_is_not_cached_as_success(tmp_path):
    """A rejected/failed import must record the failure plainly and must not
    masquerade as a completed channel on the next import."""
    cid, cdir, _, cached = ch.prepare_channel(
        S2P.read_bytes(), "chan_2port.s2p",
        params={"fit_bandwidths_ghz": [12], "pole_grid": [[1, 8]], "passivity_limit": 0.10},
        root=tmp_path,
    )
    assert cached is False
    meta = ch.run_fit(cdir)
    assert meta["state"] == "done" and meta["status"] == "failed"
    assert "no passive fit" in meta["reason"]
    assert not (cdir / "channel.sp").exists()
    _, _, _, cached_again = ch.prepare_channel(
        S2P.read_bytes(), "chan_2port.s2p",
        params={"fit_bandwidths_ghz": [12], "pole_grid": [[1, 8]], "passivity_limit": 0.10},
        root=tmp_path,
    )
    assert cached_again is False  # failures are retried, not served from cache


# ---------------------------------------------------------------------------
# Artifact contract (what a future Circuit_Builder run deck will consume)


def test_artifact_contract_is_complete(imported_s2p, imported_s4p):
    for res, ports, name in ((imported_s2p[0], 2, "ddr_chan"), (imported_s4p[0], 4, "ddr_chan4")):
        cdir = Path(res["spice_subckt_path"]).parent
        c = ch.artifact_contract(res["meta"], cdir)
        assert c["nports"] == ports and c["subckt_name"] == name
        assert c["port_order"] == [f"p{i + 1}" for i in range(ports)]
        assert Path(c["subckt_path"]).is_file() and Path(c["cursors_path"]).is_file()
        assert Path(c["taps_hex_path"]).is_file()
        assert Path(c["subckt_path"]).is_absolute()
        assert c["passive"] is True
        assert c["ui_s"] == pytest.approx(312.5e-12)
        assert c["reference_z"] == pytest.approx(50.0)
        assert isinstance(c["cursor_index_of_main"], int)


# ---------------------------------------------------------------------------
# API


@pytest.fixture()
def api(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"working_dir": str(tmp_path / "workdir")}))
    monkeypatch.setenv("ANALOG_SPEC_TOOL_SETTINGS", str(settings_file))
    import main

    return main, TestClient(main.app)


def _wait_done(client, cid, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/channels/{cid}").json()
        if body.get("state") == "done":
            return body
        time.sleep(0.3)
    raise AssertionError(f"channel {cid} never finished")


def test_api_import_list_and_fetch(api):
    main, client = api
    res = client.post("/api/channels", json={
        "content": S2P.read_text(), "filename": "chan_2port.s2p",
        "name": "api 2-port", "fit_params": FAST_PARAMS,
    })
    assert res.status_code == 200
    cid = res.json()["channel_id"]
    assert res.json()["cached"] is False

    body = _wait_done(client, cid)
    assert body["status"] == "success"
    assert body["metrics"]["passive"] is True
    assert body["artifact_contract"]["subckt_name"] == "ddr_chan"

    listing = client.get("/api/channels").json()
    assert [c["channel_id"] for c in listing] == [cid]
    assert listing[0]["name"] == "api 2-port" and listing[0]["passive"] is True

    sp = client.get(f"/api/channels/{cid}/file/channel.sp")
    assert sp.status_code == 200 and ".SUBCKT ddr_chan" in sp.text
    assert client.get(f"/api/channels/{cid}/file/cursors.txt").status_code == 200
    assert client.get(f"/api/channels/{cid}/file/taps.hex").status_code == 200
    assert client.get(f"/api/channels/{cid}/file/fit.log").status_code == 200

    # Artifacts landed under <working_dir>/channels/<channel_id>/.
    assert Path(body["dir"]).parent.name == "channels"
    assert Path(body["dir"]).name == cid


def test_api_second_import_is_a_cache_hit(api):
    main, client = api
    payload = {"content": S2P.read_text(), "filename": "chan_2port.s2p",
               "fit_params": FAST_PARAMS}
    cid = client.post("/api/channels", json=payload).json()["channel_id"]
    _wait_done(client, cid)
    again = client.post("/api/channels", json=payload).json()
    assert again["cached"] is True and again["channel_id"] == cid
    assert again["status"] == "success"


def test_api_server_side_path_import(api):
    main, client = api
    res = client.post("/api/channels", json={
        "path": str(S4P), "fit_params": FAST_PARAMS_4P,
    })
    assert res.status_code == 200
    body = _wait_done(client, res.json()["channel_id"])
    assert body["status"] == "success" and body["nports"] == 4
    assert body["metrics"]["subckt_name"] == "ddr_chan4"


def test_api_rejects_bad_requests(api):
    main, client = api
    assert client.post("/api/channels", json={}).status_code == 400
    assert client.post("/api/channels", json={"path": "relative.s2p"}).status_code == 400
    assert client.post("/api/channels", json={"path": "/nope/missing.s2p"}).status_code == 400
    assert client.post("/api/channels", json={
        "content": "x", "filename": "thing.s3p"}).status_code == 400
    assert client.get("/api/channels/nosuchchannel").status_code == 404


def test_api_file_route_blocks_traversal(api):
    main, client = api
    cid = client.post("/api/channels", json={
        "content": S2P.read_text(), "filename": "chan_2port.s2p",
        "fit_params": FAST_PARAMS}).json()["channel_id"]
    _wait_done(client, cid)
    assert client.get(f"/api/channels/{cid}/file/..%2Fmeta.json").status_code in (400, 404)
    assert client.get(f"/api/channels/{cid}/file/nothere.txt").status_code == 404


def test_api_channels_routes_are_auth_gated(api):
    """Every /api route must sit behind the shared-secret gate (a real remote
    peer, i.e. not loopback/testclient, needs a token)."""
    main, client = api
    remote = TestClient(main.app, client=("203.0.113.5", 54321))
    assert remote.get("/api/channels").status_code == 401
    assert remote.post("/api/channels", json={"path": str(S2P)}).status_code == 401
    assert remote.get("/api/channels/anything").status_code == 401

    import settings as settings_mod

    token = settings_mod.auth_token()
    assert remote.get("/api/channels", headers={"X-Auth-Token": token}).status_code == 200


def test_api_reports_a_failed_fit_plainly(api):
    """The one real failure mode: no usable model comes out. It must surface
    as a failed channel with a reason, not as a half-empty success."""
    main, client = api
    cid = client.post("/api/channels", json={
        "content": S2P.read_text(), "filename": "chan_2port.s2p",
        "fit_params": {"fit_bandwidths_ghz": [12], "pole_grid": [[1, 8]],
                       "passivity_limit": 0.10},
    }).json()["channel_id"]
    body = _wait_done(client, cid)
    assert body["status"] == "failed"
    assert "no passive fit" in body["reason"]
    assert body["artifact_contract"] is None
    assert client.get("/api/channels").json()[0]["status"] == "failed"
