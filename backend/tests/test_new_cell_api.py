"""API-level tests for the "draw a new schematic by hand" routes (2026-09):
POST /api/schematic/new-cell, POST /api/xschem/sessions/cell, POST
/api/schematic/capture (backend/main.py) - the HTTP surface over
backend/schematics.py's create_blank_cell / sch_and_cwd_for_cell /
capture_manual_cell, already unit-tested directly against the real xschem
binary in tests/test_manual_schematic.py. These tests stub out the
subprocess-spawning bits (xschem_session_mod.start_session, capture_manual_cell)
so they run fast and without needing Xvfb/x11vnc, exercising just the
routing/validation/status-code contract.

Same throwaway-settings-file / plain-TestClient (loopback-exempt "testclient"
peer) convention as every other suite here - see tests/test_auth.py's
docstring.

Run:  cd backend && .venv/bin/python3 -m pytest tests/test_new_cell_api.py -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    wd = tmp_path / "workdir"
    settings_file.write_text(json.dumps({"working_dir": str(wd)}))
    monkeypatch.setenv("ANALOG_SPEC_TOOL_SETTINGS", str(settings_file))
    return settings_file


@pytest.fixture()
def app_module(workdir):
    import main

    return main


@pytest.fixture()
def client(app_module):
    return TestClient(app_module.app)


def test_new_cell_creates_blank_schematic(client):
    r = client.post(
        "/api/schematic/new-cell",
        json={"library": "ddr_afe", "cell": "bandgap_reference", "topology": "bandgap_reference", "label": "Vref Gen"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["library"] == "ddr_afe"
    assert body["cell"] == "bandgap_reference"
    assert body["sch"] == "bandgap_reference.sch"

    resolved = client.get("/api/schematic/resolve/bandgap_reference").json()
    assert resolved["found"] is False, "no PNG yet - must not resolve until captured"


def test_new_cell_refuses_to_clobber_existing_sch(client):
    body = {"library": "ddr_afe", "cell": "odt_cell", "topology": "input_termination_buffer"}
    r1 = client.post("/api/schematic/new-cell", json=body)
    assert r1.status_code == 200, r1.text

    r2 = client.post("/api/schematic/new-cell", json=body)
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert detail["library"] == "ddr_afe"
    assert detail["cell"] == "odt_cell"


@pytest.mark.parametrize("bad_library", ["", "../etc", "1bad", "a/b", "x" * 65])
def test_new_cell_rejects_bad_library_name(client, bad_library):
    r = client.post(
        "/api/schematic/new-cell",
        json={"library": bad_library, "cell": "cell1", "topology": "bandgap_reference"},
    )
    assert r.status_code == 422, r.text


def test_new_cell_rejects_bad_topology_name(client):
    r = client.post(
        "/api/schematic/new-cell",
        json={"library": "ddr_afe", "cell": "cell1", "topology": "../etc"},
    )
    assert r.status_code == 422, r.text


def test_cell_session_404_for_nonexistent_cell(client):
    r = client.post("/api/xschem/sessions/cell", json={"library": "nope", "cell": "nope"})
    assert r.status_code == 404, r.text


def test_cell_session_starts_on_the_new_blank_cell(client, app_module, monkeypatch):
    client.post(
        "/api/schematic/new-cell",
        json={"library": "ddr_afe", "cell": "pll_cell", "topology": "pll"},
    )
    captured_args = {}

    def fake_start_session(cwd, sch_path, label, resolution=None):
        captured_args["cwd"] = cwd
        captured_args["sch_path"] = sch_path
        captured_args["label"] = label
        captured_args["resolution"] = resolution
        return {
            "session_id": "fake123", "label": label, "vnc_password": "pw", "reused": False,
            "resolution": resolution,
        }

    monkeypatch.setattr(app_module.xschem_session_mod, "start_session", fake_start_session)
    r = client.post("/api/xschem/sessions/cell", json={"library": "ddr_afe", "cell": "pll_cell"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "fake123"
    assert body["label"] == "ddr_afe/pll_cell"
    assert captured_args["sch_path"].name == "pll_cell.sch"
    # No resolution sent in the request body: the route's own default
    # (xschem_session_mod.DEFAULT_RESOLUTION) is what's threaded through.
    assert captured_args["resolution"] == app_module.xschem_session_mod.DEFAULT_RESOLUTION


def test_capture_422_for_nonexistent_cell(client):
    r = client.post("/api/schematic/capture", json={"library": "nope", "cell": "nope"})
    assert r.status_code == 422, r.text


def test_capture_round_trip_makes_the_block_resolve(client, app_module, monkeypatch):
    """Stubs schematics.capture_manual_cell itself (the subprocess-spawning
    part is unit-tested for real in tests/test_manual_schematic.py) - proves
    the ROUTE wiring: a successful capture response, and that resolve_schematic
    (a real call, not stubbed) finds the cell afterward because the stub
    writes a real PNG + provenance exactly like the real implementation does."""
    client.post(
        "/api/schematic/new-cell",
        json={"library": "ddr_afe", "cell": "vref_cell", "topology": "bandgap_reference"},
    )

    def fake_capture(library, cell):
        from pathlib import Path

        import settings as settings_mod

        cell_dir = settings_mod.libraries_root() / library / cell
        (cell_dir / f"{cell}.png").write_bytes(b"\x89PNG fake")
        prov_path = cell_dir / "provenance.json"
        prov = json.loads(prov_path.read_text())
        prov["filed_at"] = "2026-09-04T12:00:00+00:00"
        prov["views"] = [f"{cell}.sch", f"{cell}.png"]
        prov_path.write_text(json.dumps(prov))
        return {
            "library": library, "cell": cell, "dir": str(cell_dir),
            "sch": f"{cell}.sch", "png": f"{cell}.png", "netlist": None,
            "netlist_warning": "stubbed - see tests/test_manual_schematic.py for the real path",
        }

    monkeypatch.setattr(app_module, "capture_manual_cell", fake_capture)
    r = client.post("/api/schematic/capture", json={"library": "ddr_afe", "cell": "vref_cell"})
    assert r.status_code == 200, r.text
    assert r.json()["netlist_warning"]

    resolved = client.get("/api/schematic/resolve/bandgap_reference").json()
    assert resolved["found"] is True
    assert resolved["library"] == "ddr_afe"
    assert resolved["cell"] == "vref_cell"
