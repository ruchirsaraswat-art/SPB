"""
Unit/integration tests for the digital-architecture + AFE<->digital interface
workspace backend (2026-08-23 spec: backend/digital_blocks.py, arch_chat
view="digital", backend/interfaces.py, backend/if_chat.py, and the
/api/digital_blocks, /api/interfaces, /api/if_chat endpoints). All claude
invocations are STUBBED - running this suite costs nothing. Style mirrors
test_arch_chat.py.

Run:  cd backend && .venv/bin/python3 -m pytest test_digital_if.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import if_chat
from arch_chat import normalize_architecture, validate_patch
from digital_blocks import (
    ALL_DIGITAL_BLOCK_VALUES,
    DIGITAL_BLOCK_GROUPS,
    DIGITAL_BLOCKS_BY_PHY,
)
from interfaces import (
    apply_if_patch,
    normalize_interface,
    validate_interface,
)
from topologies import ALL_TOPOLOGY_VALUES

# The shipped per-PHY defaults are the frontend's built-ins AND the backend's
# round-trip fixtures - single source of truth, one JSON per PHY.
DEFAULTS_DIR = Path(__file__).resolve().parent.parent / "frontend" / "src" / "defaultInterfaces"


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    wd = tmp_path / "workdir"
    settings_file.write_text(json.dumps({"working_dir": str(wd)}))
    monkeypatch.setenv("ANALOG_SPEC_TOOL_SETTINGS", str(settings_file))
    return wd


@pytest.fixture()
def client(workdir):
    import main

    return TestClient(main.app)


def load_default(phy: str) -> dict:
    return json.loads((DEFAULTS_DIR / f"{phy}.json").read_text())


# ---------------------------------------------------------------------------
# Digital block catalog


def test_catalog_shape_matches_topologies_contract():
    assert DIGITAL_BLOCK_GROUPS, "catalog must not be empty"
    for group in DIGITAL_BLOCK_GROUPS:
        assert group["group"] and isinstance(group["options"], list)
        for opt in group["options"]:
            assert opt["value"] and opt["label"]
            assert isinstance(opt["fields"], list)
            for f in opt["fields"]:
                assert f["name"] and f["label"] and "unit" in f
            assert set(opt["models"]) == {"veriloga", "verilog", "rnm"}


def test_catalog_model_applicability():
    rnm_true = set()
    for group in DIGITAL_BLOCK_GROUPS:
        for opt in group["options"]:
            models = opt["models"]
            # Verilog-A never applies to synchronous digital blocks.
            assert isinstance(models["veriloga"], str)
            assert "no compact-model behavior" in models["veriloga"]
            assert models["verilog"] is True
            if models["rnm"] is True:
                rnm_true.add(opt["value"])
            else:
                assert "pure bit-level block" in models["rnm"]
    # Exactly the cal/training/monitor blocks that touch real-valued AFE
    # quantities get RNM.
    assert rnm_true == {
        "link_training_fsm", "power_state_fsm", "cal_engine", "eye_monitor_ctrl",
        "ddr_training_engine", "zq_cal_fsm", "d2d_link_state_fsm",
    }


def test_catalog_is_a_separate_namespace_from_analog():
    assert ALL_DIGITAL_BLOCK_VALUES  # non-empty
    assert not (ALL_DIGITAL_BLOCK_VALUES & ALL_TOPOLOGY_VALUES)


def test_by_phy_map_only_references_cataloged_blocks():
    for phy, blocks in DIGITAL_BLOCKS_BY_PHY.items():
        unknown = set(blocks) - ALL_DIGITAL_BLOCK_VALUES
        assert not unknown, f"{phy}: {unknown}"
    # hbm = the memory set + lane repair
    assert set(DIGITAL_BLOCKS_BY_PHY["hbm"]) == set(DIGITAL_BLOCKS_BY_PHY["lpddr"]) | {"lane_repair_mux"}
    assert DIGITAL_BLOCKS_BY_PHY["ddr"] == DIGITAL_BLOCKS_BY_PHY["lpddr"]


def test_digital_blocks_endpoint(client):
    r = client.get("/api/digital_blocks")
    assert r.status_code == 200
    body = r.json()
    assert {g["group"] for g in body["groups"]} == {g["group"] for g in DIGITAL_BLOCK_GROUPS}
    assert body["by_phy"]["ser-des"] == DIGITAL_BLOCKS_BY_PHY["ser-des"]
    values = {o["value"] for g in body["groups"] for o in g["options"]}
    assert values == ALL_DIGITAL_BLOCK_VALUES


# ---------------------------------------------------------------------------
# arch_chat view="digital" patch validation

DIG_ARCH = {
    "blocks": [
        {"id": "rx-align", "label": "Word Aligner", "topology": "word_aligner", "col": 1, "row": 0},
        {"id": "csr", "label": "CSR", "topology": "csr_regfile", "col": 2, "row": 0},
        {"id": "afe-rx-if", "label": "AFE RX IF", "topology": None, "kind": "iface", "col": 0, "row": 0},
    ],
    "edges": [{"from": "afe-rx-if", "to": "rx-align"}],
}


def dig_norm():
    return normalize_architecture(DIG_ARCH)


def test_digital_view_accepts_digital_blocks():
    ops, valid = validate_patch(
        {"ops": [
            {"op": "add_block", "id": "fifo", "label": "Elastic FIFO", "topology": "elastic_fifo"},
            {"op": "add_connection", "from": "rx-align", "to": "fifo"},
            {"op": "set_topology", "id": "csr", "topology": "protocol_engine"},
        ]},
        dig_norm(), view="digital", phy_type="ser-des",
    )
    assert valid is True
    assert all("error" not in op for op in ops)
    assert all("warning" not in op for op in ops)  # elastic_fifo is in the ser-des set


def test_digital_view_rejects_analog_topologies_and_vice_versa():
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "x", "label": "X", "topology": "ctle"}]},
        dig_norm(), view="digital",
    )
    assert valid is False and "unknown digital block 'ctle'" in ops[0]["error"]

    afe = normalize_architecture(
        {"blocks": [{"id": "rx", "label": "RX", "topology": "ctle"}], "edges": []}
    )
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "y", "label": "Y", "topology": "elastic_fifo"}]},
        afe, view="afe",
    )
    assert valid is False and "unknown topology 'elastic_fifo'" in ops[0]["error"]


def test_digital_view_kind_iface_allowed_pad_rejected():
    ops, valid = validate_patch(
        {"ops": [
            {"op": "add_block", "id": "core-if", "label": "Core IF", "topology": None, "kind": "iface"},
            {"op": "add_block", "id": "p", "label": "Pad", "topology": None, "kind": "pad"},
        ]},
        dig_norm(), view="digital",
    )
    assert "error" not in ops[0]
    assert "kind 'pad' is not valid in the digital view" in ops[1]["error"]
    assert valid is False


def test_afe_view_kind_pad_allowed_iface_rejected():
    afe = normalize_architecture({"blocks": [{"id": "rx"}], "edges": []})
    ops, valid = validate_patch(
        {"ops": [
            {"op": "add_block", "id": "p", "label": "Pad", "topology": None, "kind": "pad"},
            {"op": "add_block", "id": "i", "label": "IF", "topology": None, "kind": "iface"},
        ]},
        afe, view="afe",
    )
    assert "error" not in ops[0]
    assert "kind 'iface' is not valid in the afe view" in ops[1]["error"]
    assert valid is False


def test_digital_view_off_phy_block_soft_warning():
    # dfi_command_path is not in the ser-des set: allowed, but flagged.
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "cmd", "label": "CMD", "topology": "dfi_command_path"}]},
        dig_norm(), view="digital", phy_type="ser-des",
    )
    assert valid is True
    assert "not in the usual digital block set for a ser-des PHY" in ops[0]["warning"]


def _stub_result(raw_text):
    import arch_chat

    return arch_chat.ArchChatResult(
        status="success", raw_text=raw_text, session_id="cli-sess", cost_usd=0.01, duration_ms=1
    )


def test_arch_chat_endpoint_digital_view(client, workdir, monkeypatch):
    import arch_chat

    prompts = []

    def stub(prompt, log_dir, log_stem, timeout_s=0):
        prompts.append(prompt)
        return _stub_result(
            'Add a FIFO.\n```json\n{"ops": [{"op": "add_block", "id": "fifo", '
            '"label": "Elastic FIFO", "topology": "elastic_fifo"}]}\n```'
        )

    monkeypatch.setattr(arch_chat, "run_arch_consultant", stub)
    r = client.post(
        "/api/arch_chat",
        json={
            "session_id": "dig1",
            "message": "add an elastic fifo",
            "architecture": DIG_ARCH,
            "phy_type": "ser-des",
            "view": "digital",
            "afe_architecture": {"blocks": [{"id": "deser", "label": "Deser", "topology": "deserializer"}], "edges": []},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patch_valid"] is True and body["patch_warnings"] == []
    # Digital framing + read-only AFE context made it into the prompt.
    assert "DIGITAL MICROARCHITECTURE CONSULTANT" in prompts[0]
    assert "READ-ONLY" in prompts[0] and '"deser"' in prompts[0]


def test_arch_chat_endpoint_digital_view_rejects_analog_patch(client, workdir, monkeypatch):
    import arch_chat

    monkeypatch.setattr(
        arch_chat,
        "run_arch_consultant",
        lambda *a, **k: _stub_result(
            'Sure.\n```json\n{"ops": [{"op": "add_block", "id": "v", "label": "VGA", "topology": "vga"}]}\n```'
        ),
    )
    r = client.post(
        "/api/arch_chat",
        json={"session_id": "dig2", "message": "add vga", "architecture": DIG_ARCH, "view": "digital"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch_valid"] is False
    assert "unknown digital block 'vga'" in body["patch"]["ops"][0]["error"]


def test_arch_chat_afe_view_unchanged_default(client, workdir, monkeypatch):
    # No "view" in the request at all = the pre-existing analog behavior.
    import arch_chat

    monkeypatch.setattr(
        arch_chat,
        "run_arch_consultant",
        lambda *a, **k: _stub_result(
            'OK.\n```json\n{"ops": [{"op": "add_block", "id": "v", "label": "VGA", "topology": "vga"}]}\n```'
        ),
    )
    r = client.post(
        "/api/arch_chat",
        json={
            "session_id": "afe1",
            "message": "add vga",
            "architecture": {"blocks": [{"id": "rx", "label": "RX", "topology": "ctle"}], "edges": []},
        },
    )
    assert r.status_code == 200
    assert r.json()["patch_valid"] is True


# ---------------------------------------------------------------------------
# Interface validation: hard errors vs soft warnings


def minimal_if(**overrides):
    base = {
        "name": "t",
        "phy_type": "ser-des",
        "clock_domains": [{"name": "clk_a", "freq_mhz": 100, "owner": "external"}],
        "signals": [
            {"name": "sig_a", "direction": "a2d", "width": 8, "clock_domain": "clk_a",
             "rate": "word", "rate_mhz": 100, "group": "data", "handshake": "none"},
        ],
        "cdc_points": [],
        "reset_sequence": [],
        "power_sequence": [],
    }
    base.update(overrides)
    return normalize_interface(base)


def test_minimal_interface_clean():
    errors, warnings = validate_interface(minimal_if())
    assert errors == [] and warnings == []


@pytest.mark.parametrize(
    "mutate,expect",
    [
        (lambda s: s.update(name="BadName"), "invalid signal name"),
        (lambda s: s.update(name="9sig"), "invalid signal name"),
        (lambda s: s.update(direction="up"), "unknown direction"),
        (lambda s: s.update(rate="fast"), "unknown rate"),
        (lambda s: s.update(group="misc"), "unknown group"),
        (lambda s: s.update(handshake="wave"), "unknown handshake"),
        (lambda s: s.update(clock_domain="clk_ghost"), "unknown clock_domain"),
        (lambda s: s.update(width=0), "width must be an integer >= 1"),
        (lambda s: s.update(width=2.5), "width must be an integer >= 1"),
        (lambda s: s.update(rate_mhz=None), "rate_mhz is required"),
    ],
)
def test_signal_hard_errors(mutate, expect):
    iface = minimal_if()
    mutate(iface["signals"][0])
    errors, _ = validate_interface(iface)
    assert any(expect in e for e in errors), errors


def test_duplicate_names_hard_error():
    iface = minimal_if()
    iface["signals"].append(dict(iface["signals"][0]))
    errors, _ = validate_interface(iface)
    assert any("duplicate signal name" in e for e in errors)

    iface = minimal_if()
    iface["clock_domains"].append({"name": "clk_a"})
    errors, _ = validate_interface(iface)
    assert any("duplicate clock domain" in e for e in errors)


def test_async_reserved_domain_name():
    iface = minimal_if()
    iface["clock_domains"].append({"name": "async"})
    errors, _ = validate_interface(iface)
    assert any("'async' is reserved" in e for e in errors)


def test_cdc_dangling_references_hard_error():
    iface = minimal_if(cdc_points=[
        {"signal": "ghost", "from_domain": "clk_a", "to_domain": "clk_a", "strategy": "2ff"},
        {"signal": "sig_a", "from_domain": "clk_nope", "to_domain": "clk_a", "strategy": "2ff"},
    ])
    errors, _ = validate_interface(iface)
    assert any("nonexistent signal 'ghost'" in e for e in errors)
    assert any("from_domain 'clk_nope'" in e for e in errors)


def test_soft_warning_line_rate():
    iface = minimal_if()
    iface["signals"][0].update(rate="line", rate_mhz=None)
    errors, warnings = validate_interface(iface)
    assert errors == []
    assert any("line-rate" in w for w in warnings)


def test_soft_warning_async_without_cdc():
    iface = minimal_if()
    iface["signals"][0].update(clock_domain="async", rate="async_event", rate_mhz=None)
    errors, warnings = validate_interface(iface)
    assert errors == []
    assert any("no cdc_points entry" in w for w in warnings)
    # 2ff_sync handshake OR a cdc entry silences it.
    iface["signals"][0]["handshake"] = "2ff_sync"
    _, warnings = validate_interface(iface)
    assert warnings == []
    iface["signals"][0]["handshake"] = "none"
    iface["cdc_points"] = [
        {"signal": "sig_a", "from_domain": "async", "to_domain": "clk_a", "strategy": "2ff"}
    ]
    _, warnings = validate_interface(iface)
    assert warnings == []


def test_soft_warning_pulse_without_cdc():
    iface = minimal_if()
    iface["signals"][0]["handshake"] = "pulse"
    errors, warnings = validate_interface(iface)
    assert errors == []
    assert any("pulse" in w and "stretched/synchronized" in w for w in warnings)


def test_soft_warning_word_clock_above_200mhz():
    iface = minimal_if()
    iface["signals"][0]["rate_mhz"] = 250
    errors, warnings = validate_interface(iface)
    assert errors == []
    assert any("aggressive for sky130 synthesized logic" in w for w in warnings)


def test_dangling_block_links_warn_only_with_arch_context():
    iface = minimal_if()
    iface["signals"][0].update(afe_block="gone-afe", digital_block="gone-dig")
    # Without the diagrams: silent (a bare save can't know the block sets).
    errors, warnings = validate_interface(iface)
    assert errors == [] and warnings == []
    afe = {"blocks": [{"id": "rx"}], "edges": []}
    dig = {"blocks": [{"id": "csr"}], "edges": []}
    errors, warnings = validate_interface(iface, afe, dig)
    assert errors == []
    assert any("afe_block 'gone-afe'" in w for w in warnings)
    assert any("digital_block 'gone-dig'" in w for w in warnings)


# ---------------------------------------------------------------------------
# Interface patch ops (apply_if_patch)


def test_ordered_ops_add_signal_then_cdc():
    ops, valid, result = apply_if_patch(
        {"ops": [
            {"op": "add_signal", "signal": {
                "name": "sig_b", "direction": "a2d", "width": 1, "clock_domain": "async",
                "rate": "async_event", "group": "status", "handshake": "2ff_sync",
                "meaning": "new status"}},
            {"op": "add_cdc", "cdc": {"signal": "sig_b", "from_domain": "async",
                                      "to_domain": "clk_a", "strategy": "2ff"}},
        ]},
        minimal_if(),
    )
    assert valid is True, ops
    assert [s["name"] for s in result["signals"]] == ["sig_a", "sig_b"]
    assert result["cdc_points"][0]["signal"] == "sig_b"


def test_add_signal_duplicate_and_invalid():
    ops, valid, _ = apply_if_patch(
        {"ops": [
            {"op": "add_signal", "signal": {"name": "sig_a", "direction": "a2d", "width": 1,
                                            "clock_domain": "clk_a", "rate": "quasi_static",
                                            "group": "control", "handshake": "level"}},
            {"op": "add_signal", "signal": {"name": "sig_c", "direction": "sideways", "width": 1,
                                            "clock_domain": "clk_a", "rate": "quasi_static",
                                            "group": "control", "handshake": "level"}},
        ]},
        minimal_if(),
    )
    assert valid is False
    assert "duplicate signal name" in ops[0]["error"]
    assert "unknown direction" in ops[1]["error"]


def test_remove_signal_cascades_cdc():
    iface = minimal_if(cdc_points=[
        {"signal": "sig_a", "from_domain": "clk_a", "to_domain": "async", "strategy": "2ff"}
    ])
    ops, valid, result = apply_if_patch({"ops": [{"op": "remove_signal", "name": "sig_a"}]}, iface)
    assert valid is True
    assert result["signals"] == [] and result["cdc_points"] == []

    ops, valid, _ = apply_if_patch({"ops": [{"op": "remove_signal", "name": "ghost"}]}, minimal_if())
    assert valid is False and "no signal named 'ghost'" in ops[0]["error"]


def test_update_signal_rename_cascades_and_validates():
    iface = minimal_if(cdc_points=[
        {"signal": "sig_a", "from_domain": "clk_a", "to_domain": "async", "strategy": "2ff"}
    ])
    ops, valid, result = apply_if_patch(
        {"ops": [{"op": "update_signal", "name": "sig_a", "fields": {"name": "sig_z", "width": 4}}]},
        iface,
    )
    assert valid is True
    assert result["signals"][0]["name"] == "sig_z" and result["signals"][0]["width"] == 4
    assert result["cdc_points"][0]["signal"] == "sig_z"
    # The patched-in result still validates clean end-to-end.
    errors, _ = validate_interface(result)
    assert errors == []

    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "update_signal", "name": "sig_a", "fields": {"rate": "warp"}}]}, minimal_if()
    )
    assert valid is False and "unknown rate" in ops[0]["error"]


def test_clock_domain_ops():
    # remove rejected while referenced; fine after the signal moves.
    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "remove_clock_domain", "name": "clk_a"}]}, minimal_if()
    )
    assert valid is False and "still referenced" in ops[0]["error"]

    ops, valid, result = apply_if_patch(
        {"ops": [
            {"op": "add_clock_domain", "domain": {"name": "clk_b", "freq_mhz": 50, "owner": "afe"}},
            {"op": "update_signal", "name": "sig_a", "fields": {"clock_domain": "clk_b", "rate_mhz": 50}},
            {"op": "remove_clock_domain", "name": "clk_a"},
        ]},
        minimal_if(),
    )
    assert valid is True, ops
    assert [d["name"] for d in result["clock_domains"]] == ["clk_b"]

    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "add_clock_domain", "domain": {"name": "clk_a"}}]}, minimal_if()
    )
    assert valid is False and "already exists" in ops[0]["error"]

    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "add_clock_domain", "domain": {"name": "async"}}]}, minimal_if()
    )
    assert valid is False and "invalid domain name" in ops[0]["error"]


def test_update_clock_domain_rename_cascades():
    iface = minimal_if(cdc_points=[
        {"signal": "sig_a", "from_domain": "clk_a", "to_domain": "async", "strategy": "2ff"}
    ])
    ops, valid, result = apply_if_patch(
        {"ops": [{"op": "update_clock_domain", "name": "clk_a", "fields": {"name": "clk_main", "freq_mhz": 125}}]},
        iface,
    )
    assert valid is True
    assert result["clock_domains"][0]["name"] == "clk_main"
    assert result["signals"][0]["clock_domain"] == "clk_main"
    assert result["cdc_points"][0]["from_domain"] == "clk_main"
    errors, _ = validate_interface(result)
    assert errors == []


def test_cdc_ops_and_sequences():
    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "add_cdc", "cdc": {"signal": "ghost", "from_domain": "x", "to_domain": "clk_a"}}]},
        minimal_if(),
    )
    assert valid is False
    assert "nonexistent signal 'ghost'" in ops[0]["error"] and "from_domain 'x'" in ops[0]["error"]

    ops, valid, _ = apply_if_patch({"ops": [{"op": "remove_cdc", "signal": "sig_a"}]}, minimal_if())
    assert valid is False and "no cdc entry" in ops[0]["error"]

    steps = [{"step": 1, "action": "Enable bias", "signal": "en_bias", "wait_for": "20 us"}]
    ops, valid, result = apply_if_patch(
        {"ops": [{"op": "set_power_sequence", "steps": steps},
                 {"op": "set_reset_sequence", "steps": []}]},
        minimal_if(power_sequence=[{"step": 1, "action": "old"}]),
    )
    assert valid is True
    assert result["power_sequence"] == steps and result["reset_sequence"] == []

    ops, valid, _ = apply_if_patch(
        {"ops": [{"op": "set_power_sequence", "steps": [{"step": 1}]}]}, minimal_if()
    )
    assert valid is False and "non-empty 'action'" in ops[0]["error"]


def test_unknown_if_op_and_malformed_patch():
    ops, valid, _ = apply_if_patch({"ops": [{"op": "teleport_signal"}]}, minimal_if())
    assert valid is False and "unknown op" in ops[0]["error"]
    ops, valid, _ = apply_if_patch(["nope"], minimal_if())
    assert valid is False


# ---------------------------------------------------------------------------
# Shipped defaults + storage round trip


@pytest.mark.parametrize("phy", ["ser-des", "lpddr", "die-to-die", "optical"])
def test_shipped_defaults_validate_clean(phy):
    iface = normalize_interface(load_default(phy))
    errors, warnings = validate_interface(iface)
    assert errors == [], errors
    assert warnings == [], warnings


def test_serdes_default_roundtrip_and_overwrite(client, workdir):
    r0 = client.get("/api/interfaces")
    assert r0.status_code == 200 and r0.json()["interfaces"] == []

    seed = load_default("ser-des")
    r1 = client.post(
        "/api/interfaces",
        json={"name": "serdes-rx-if", "phy_type": "ser-des", "label": "SerDes RX IF", "interface": seed},
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert body["name"] == "serdes-rx-if"
    assert body["warnings"] == []  # the worked example ships warning-free

    r2 = client.get("/api/interfaces")
    entries = r2.json()["interfaces"]
    assert len(entries) == 1
    e = entries[0]
    assert e["label"] == "SerDes RX IF" and e["phy_type"] == "ser-des"
    assert {s["name"] for s in e["interface"]["signals"]} == {s["name"] for s in seed["signals"]}
    assert (workdir / "interfaces" / "serdes-rx-if.json").exists()

    # Overwrite with the same name updates in place (no duplicate).
    r3 = client.post("/api/interfaces", json={"name": "serdes-rx-if", "interface": seed})
    assert r3.status_code == 200 and "updated_at" in r3.json()
    assert len(client.get("/api/interfaces").json()["interfaces"]) == 1


def test_interface_save_hard_errors_422(client, workdir):
    r = client.post("/api/interfaces", json={"name": "bad/name", "interface": load_default("ser-des")})
    assert r.status_code == 422

    broken = load_default("ser-des")
    broken["signals"][0]["direction"] = "sideways"
    r = client.post("/api/interfaces", json={"name": "ok-name", "interface": broken})
    assert r.status_code == 422
    assert "unknown direction" in r.json()["detail"]


def test_interface_save_stores_soft_warnings(client, workdir):
    seed = load_default("ser-des")
    seed["signals"][0]["rate_mhz"] = 400  # sky130 warning, not an error
    r = client.post("/api/interfaces", json={"name": "hot-if", "interface": seed})
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert any("aggressive for sky130 synthesized logic" in w for w in warnings)
    stored = json.loads((workdir / "interfaces" / "hot-if.json").read_text())
    assert stored["warnings"] == warnings


# ---------------------------------------------------------------------------
# POST /api/if_chat with a stubbed consultant


def test_if_chat_two_messages_persist_transcript(client, workdir, monkeypatch):
    replies = iter([
        "Informational answer only.",
        'Add the missing CDC.\n```json\n{"ops": [{"op": "add_cdc", "cdc": {"signal": "sig_a", '
        '"from_domain": "clk_a", "to_domain": "async", "strategy": "2ff", "notes": "level"}}]}\n```',
    ])
    prompts = []

    def stub(prompt, log_dir, log_stem, timeout_s=None):
        prompts.append(prompt)
        return _stub_result(next(replies))

    monkeypatch.setattr(if_chat, "run_if_consultant", stub)

    iface = minimal_if()
    afe = {"blocks": [{"id": "deser", "label": "Deser", "topology": "deserializer"}], "edges": []}
    dig = {"blocks": [{"id": "rx-align", "label": "Aligner", "topology": "word_aligner"}], "edges": []}
    req = {
        "session_id": "ifsess1", "message": "Is the CDC plan complete?",
        "interface": iface, "afe_architecture": afe, "digital_architecture": dig,
        "phy_type": "ser-des",
    }
    r1 = client.post("/api/if_chat", json=req)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["reply"] == "Informational answer only."
    assert body1["patch"] is None and body1["patch_valid"] is True
    assert body1["interface_warnings"] == []
    # Prompt carried the definition + both read-only architectures + vocab.
    assert '"sig_a"' in prompts[0] and '"deser"' in prompts[0] and '"rx-align"' in prompts[0]
    assert "READ-ONLY" in prompts[0] and "quasi_static" in prompts[0]

    r2 = client.post("/api/if_chat", json={**req, "message": "Add it then."})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["patch_valid"] is True
    assert [op["op"] for op in body2["patch"]["ops"]] == ["add_cdc"]
    # History flowed into the second prompt.
    assert "Is the CDC plan complete?" in prompts[1]

    tfile = workdir / "if_chats" / "ifsess1.json"
    assert tfile.exists()
    data = json.loads(tfile.read_text())
    assert [t["role"] for t in data["turns"]] == ["user", "assistant", "user", "assistant"]

    r3 = client.get("/api/if_chat/ifsess1")
    assert r3.status_code == 200 and len(r3.json()["turns"]) == 4
    # Interface transcripts are namespaced apart from arch chats.
    assert client.get("/api/arch_chat/ifsess1").json()["turns"] == []


def test_if_chat_invalid_patch_reported_not_fatal(client, workdir, monkeypatch):
    monkeypatch.setattr(
        if_chat, "run_if_consultant",
        lambda *a, **k: _stub_result(
            'Done.\n```json\n{"ops": [{"op": "remove_signal", "name": "ghost"}]}\n```'
        ),
    )
    r = client.post(
        "/api/if_chat",
        json={"session_id": "ifsess2", "message": "remove ghost", "interface": minimal_if()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Done."
    assert body["patch_valid"] is False
    assert body["patch_errors"][0]["index"] == 0
    assert "no signal named 'ghost'" in body["patch"]["ops"][0]["error"]


def test_if_chat_warnings_reflect_patched_state(client, workdir, monkeypatch):
    # A valid patch that ADDS a warning-worthy signal: interface_warnings
    # must describe the post-patch state.
    monkeypatch.setattr(
        if_chat, "run_if_consultant",
        lambda *a, **k: _stub_result(
            'Adding it.\n```json\n{"ops": [{"op": "add_signal", "signal": {"name": "irq_evt", '
            '"direction": "a2d", "width": 1, "clock_domain": "async", "rate": "async_event", '
            '"group": "status", "handshake": "pulse", "meaning": "event"}}]}\n```'
        ),
    )
    r = client.post(
        "/api/if_chat",
        json={"session_id": "ifsess3", "message": "add irq", "interface": minimal_if()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch_valid"] is True
    assert any("irq_evt" in w for w in body["interface_warnings"])


def test_if_chat_consultant_failure_is_502(client, workdir, monkeypatch):
    import arch_chat

    monkeypatch.setattr(
        if_chat, "run_if_consultant",
        lambda *a, **k: arch_chat.ArchChatResult(status="failed", reason="timed out"),
    )
    r = client.post(
        "/api/if_chat",
        json={"session_id": "ifsess4", "message": "hello", "interface": minimal_if()},
    )
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]
    assert client.get("/api/if_chat/ifsess4").json()["turns"] == []


def test_if_chat_bad_session_id_422(client, workdir):
    r = client.post(
        "/api/if_chat",
        json={"session_id": "../evil", "message": "hi", "interface": minimal_if()},
    )
    assert r.status_code == 422
