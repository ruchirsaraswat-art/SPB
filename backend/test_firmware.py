"""
Unit/integration tests for the firmware workspace backend (2026-08-23
firmware-section spec: backend/firmware_blocks.py, arch_chat view="firmware",
backend/fw_chat.py, backend/fw_generate.py, and the /api/firmware_blocks,
/api/fw_chat, /api/fw_generate endpoints). All claude invocations are
STUBBED - running this suite costs nothing; the fw_generate honesty check
runs REAL gcc + make against stub-written fixtures (including a
deliberately-broken one that must FAIL).

Run:  cd backend && .venv/bin/python3 -m pytest test_firmware.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_chat
import fw_generate as fw_generate_mod
from arch_chat import normalize_architecture, validate_patch
from digital_blocks import ALL_DIGITAL_BLOCK_VALUES
from firmware_blocks import (
    ALL_FIRMWARE_BLOCK_VALUES,
    FIRMWARE_BLOCK_GROUPS,
    FIRMWARE_BLOCKS_BY_PHY,
)
from fw_generate import compute_register_map, deliverable_files, if_c_name
from interfaces import normalize_interface
from topologies import ALL_TOPOLOGY_VALUES

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
# Firmware block catalog


def test_catalog_shape_and_no_models_key():
    assert FIRMWARE_BLOCK_GROUPS, "catalog must not be empty"
    for group in FIRMWARE_BLOCK_GROUPS:
        assert group["group"] and isinstance(group["options"], list)
        for opt in group["options"]:
            assert opt["value"] and opt["label"]
            assert isinstance(opt["fields"], list)
            for f in opt["fields"]:
                assert f["name"] and f["label"] and "unit" in f
            # THE deliberate difference from the digital catalog: no
            # behavioral-model levels apply to host-compiled C.
            assert "models" not in opt, f"{opt['value']} must not carry a models key"


def test_catalog_is_a_separate_namespace():
    assert ALL_FIRMWARE_BLOCK_VALUES
    assert not (ALL_FIRMWARE_BLOCK_VALUES & ALL_TOPOLOGY_VALUES)
    assert not (ALL_FIRMWARE_BLOCK_VALUES & ALL_DIGITAL_BLOCK_VALUES)


def test_by_phy_map_only_references_cataloged_blocks():
    for phy, blocks in FIRMWARE_BLOCKS_BY_PHY.items():
        unknown = set(blocks) - ALL_FIRMWARE_BLOCK_VALUES
        assert not unknown, f"{phy}: {unknown}"
    assert set(FIRMWARE_BLOCKS_BY_PHY["hbm"]) == set(FIRMWARE_BLOCKS_BY_PHY["lpddr"]) | {
        "lane_repair_service"
    }
    assert FIRMWARE_BLOCKS_BY_PHY["ddr"] == FIRMWARE_BLOCKS_BY_PHY["lpddr"]
    # ser-des and optical share the eye/cal-centric service set.
    assert FIRMWARE_BLOCKS_BY_PHY["optical"] == FIRMWARE_BLOCKS_BY_PHY["ser-des"]


def test_firmware_blocks_endpoint(client):
    r = client.get("/api/firmware_blocks")
    assert r.status_code == 200
    body = r.json()
    values = {o["value"] for g in body["groups"] for o in g["options"]}
    assert values == ALL_FIRMWARE_BLOCK_VALUES
    assert body["by_phy"]["ser-des"] == FIRMWARE_BLOCKS_BY_PHY["ser-des"]
    # Honesty note about why there are no model levels.
    assert "host-compiled" in body["note"] and "not by iverilog" in body["note"]
    for g in body["groups"]:
        for o in g["options"]:
            assert "models" not in o


# ---------------------------------------------------------------------------
# arch_chat view="firmware" patch validation

FW_ARCH = {
    "blocks": [
        {"id": "api", "label": "Host API", "topology": "host_api_service", "col": 2, "row": 1},
        {"id": "drv", "label": "Reg Driver", "topology": "reg_access_driver", "col": 2, "row": 3},
        {"id": "csr-if", "label": "Controller CSRs", "topology": None, "kind": "iface", "col": 2, "row": 4},
    ],
    "edges": [{"from": "drv", "to": "csr-if"}],
}


def fw_norm():
    return normalize_architecture(FW_ARCH)


def test_firmware_view_accepts_firmware_blocks():
    ops, valid = validate_patch(
        {"ops": [
            {"op": "add_block", "id": "boot", "label": "Boot Seq", "topology": "boot_init_sequencer"},
            {"op": "add_connection", "from": "api", "to": "boot"},
            {"op": "add_connection", "from": "boot", "to": "drv"},
        ]},
        fw_norm(), view="firmware", phy_type="ser-des",
    )
    assert valid is True
    assert all("error" not in op and "warning" not in op for op in ops)


def test_firmware_view_rejects_other_catalogs():
    for topo, noun in (("ctle", "firmware block"), ("elastic_fifo", "firmware block")):
        ops, valid = validate_patch(
            {"ops": [{"op": "add_block", "id": "x", "label": "X", "topology": topo}]},
            fw_norm(), view="firmware",
        )
        assert valid is False
        assert f"unknown {noun} '{topo}'" in ops[0]["error"]
    # ...and firmware blocks don't leak into the other views.
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "y", "label": "Y", "topology": "cal_supervisor"}]},
        normalize_architecture({"blocks": [{"id": "a"}], "edges": []}), view="digital",
    )
    assert valid is False and "unknown digital block 'cal_supervisor'" in ops[0]["error"]


def test_firmware_view_kind_iface_allowed_pad_rejected():
    ops, valid = validate_patch(
        {"ops": [
            {"op": "add_block", "id": "host-if", "label": "Host IF", "topology": None, "kind": "iface"},
            {"op": "add_block", "id": "p", "label": "Pad", "topology": None, "kind": "pad"},
        ]},
        fw_norm(), view="firmware",
    )
    assert "error" not in ops[0]
    assert "kind 'pad' is not valid in the firmware view" in ops[1]["error"]
    assert valid is False


def test_firmware_view_off_phy_block_soft_warning():
    # zq_recal_service is a memory-PHY service: allowed on ser-des, flagged.
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "zq", "label": "ZQ", "topology": "zq_recal_service"}]},
        fw_norm(), view="firmware", phy_type="ser-des",
    )
    assert valid is True
    assert "not in the usual firmware block set for a ser-des PHY" in ops[0]["warning"]


# ---------------------------------------------------------------------------
# POST /api/fw_chat with a stubbed consultant


def _stub_result(raw_text):
    import arch_chat

    return arch_chat.ArchChatResult(
        status="success", raw_text=raw_text, session_id="cli-sess", cost_usd=0.01, duration_ms=1
    )


def minimal_if(**overrides):
    base = {
        "name": "t",
        "phy_type": "ser-des",
        "clock_domains": [{"name": "clk_a", "freq_mhz": 100, "owner": "external"}],
        "signals": [
            {"name": "trim_a", "direction": "d2a", "width": 4, "clock_domain": "clk_a",
             "rate": "quasi_static", "group": "control", "handshake": "level"},
            {"name": "lock_a", "direction": "a2d", "width": 1, "clock_domain": "clk_a",
             "rate": "quasi_static", "group": "status", "handshake": "level"},
            {"name": "clk_word", "direction": "a2d", "width": 1, "clock_domain": "clk_a",
             "rate": "word", "rate_mhz": 100, "group": "clock", "handshake": "none"},
            {"name": "rst_ext", "direction": "ext", "width": 1, "clock_domain": "clk_a",
             "rate": "quasi_static", "group": "reset", "handshake": "level"},
            {"name": "pdata", "direction": "a2d", "width": 16, "clock_domain": "clk_a",
             "rate": "word", "rate_mhz": 100, "group": "data", "handshake": "none"},
        ],
        "cdc_points": [],
        "reset_sequence": [],
        "power_sequence": [
            {"step": 1, "action": "Enable bias", "signal": "trim_a", "wait_for": "20 us settle"},
        ],
    }
    base.update(overrides)
    return normalize_interface(base)


def test_fw_chat_persists_transcript_and_carries_context(client, workdir, monkeypatch):
    replies = iter([
        "Informational answer only.",
        'Add a boot sequencer.\n```json\n{"ops": [{"op": "add_block", "id": "boot", '
        '"label": "Boot Seq", "topology": "boot_init_sequencer"}, '
        '{"op": "add_connection", "from": "boot", "to": "drv"}]}\n```',
    ])
    prompts = []

    def stub(prompt, log_dir, log_stem, timeout_s=None):
        prompts.append(prompt)
        return _stub_result(next(replies))

    monkeypatch.setattr(fw_chat, "run_fw_consultant", stub)

    req = {
        "session_id": "fwsess1",
        "message": "Should ZQ recal run from the IRQ handler?",
        "firmware_architecture": FW_ARCH,
        "interface": minimal_if(),
        "phy_type": "ser-des",
    }
    r1 = client.post("/api/fw_chat", json=req)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["reply"] == "Informational answer only."
    assert body1["patch"] is None and body1["patch_valid"] is True
    # Persona framing + firmware diagram + READ-ONLY interface context.
    p = prompts[0]
    assert "Firmware_Coder" in p and "EMBEDDED-FIRMWARE ENGINEER" in p
    assert '"drv"' in p and '"trim_a"' in p
    assert "READ-ONLY" in p and "reg_read" in p and "LAYERING VIOLATION" in p
    assert "boot_init_sequencer" in p  # firmware catalog vocabulary offered

    r2 = client.post("/api/fw_chat", json={**req, "message": "Add a boot sequencer."})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["patch_valid"] is True
    assert [op["op"] for op in body2["patch"]["ops"]] == ["add_block", "add_connection"]
    assert "Should ZQ recal run" in prompts[1]  # history flowed in

    tfile = workdir / "fw_chats" / "fwsess1.json"
    assert tfile.exists()
    data = json.loads(tfile.read_text())
    assert [t["role"] for t in data["turns"]] == ["user", "assistant", "user", "assistant"]

    r3 = client.get("/api/fw_chat/fwsess1")
    assert r3.status_code == 200 and len(r3.json()["turns"]) == 4
    # Firmware transcripts are namespaced apart from arch and interface chats.
    assert client.get("/api/arch_chat/fwsess1").json()["turns"] == []
    assert client.get("/api/if_chat/fwsess1").json()["turns"] == []


def test_fw_chat_patch_validated_against_firmware_catalog(client, workdir, monkeypatch):
    monkeypatch.setattr(
        fw_chat, "run_fw_consultant",
        lambda *a, **k: _stub_result(
            'Sure.\n```json\n{"ops": [{"op": "add_block", "id": "v", "label": "VGA", "topology": "vga"}]}\n```'
        ),
    )
    r = client.post(
        "/api/fw_chat",
        json={"session_id": "fwsess2", "message": "add vga", "firmware_architecture": FW_ARCH},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch_valid"] is False
    assert "unknown firmware block 'vga'" in body["patch"]["ops"][0]["error"]


def test_fw_chat_off_phy_soft_warning_in_response(client, workdir, monkeypatch):
    monkeypatch.setattr(
        fw_chat, "run_fw_consultant",
        lambda *a, **k: _stub_result(
            'OK.\n```json\n{"ops": [{"op": "add_block", "id": "sb", "label": "Sideband", '
            '"topology": "sideband_msg_service"}]}\n```'
        ),
    )
    r = client.post(
        "/api/fw_chat",
        json={"session_id": "fwsess3", "message": "add sideband",
              "firmware_architecture": FW_ARCH, "phy_type": "ser-des"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch_valid"] is True
    assert body["patch_warnings"] and "usual firmware block set" in body["patch_warnings"][0]["warning"]


def test_fw_chat_consultant_failure_is_502(client, workdir, monkeypatch):
    import arch_chat

    monkeypatch.setattr(
        fw_chat, "run_fw_consultant",
        lambda *a, **k: arch_chat.ArchChatResult(status="failed", reason="timed out"),
    )
    r = client.post(
        "/api/fw_chat",
        json={"session_id": "fwsess4", "message": "hello", "firmware_architecture": FW_ARCH},
    )
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]
    assert client.get("/api/fw_chat/fwsess4").json()["turns"] == []


def test_fw_chat_bad_session_id_422(client, workdir):
    r = client.post(
        "/api/fw_chat",
        json={"session_id": "../evil", "message": "hi", "firmware_architecture": FW_ARCH},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Deterministic register map rule


def test_if_c_name():
    assert if_c_name("serdes-rx-if") == "serdes_rx_if"
    assert if_c_name("lpddr-if") == "lpddr_if"
    assert if_c_name("9weird name!") == "_9weird_name_"


def test_register_map_rule_on_minimal_interface():
    regs = compute_register_map(minimal_if())
    # control then status; ext reset, clock and data rows excluded.
    assert [(r["signal"], r["offset"], r["access"]) for r in regs] == [
        ("trim_a", 0, "RW"),
        ("lock_a", 4, "RO"),
    ]


def test_register_map_serdes_default_is_exactly_15():
    """The spec's real-money smoke contract: the shipped SerDes default
    yields 15 registers - 7 control + 3 status + 1 reset (rstn_por is ext ->
    excluded) + 4 power; clock and data rows excluded."""
    iface = normalize_interface(load_default("ser-des"))
    regs = compute_register_map(iface)
    assert [r["signal"] for r in regs] == [
        "ctle_peak", "vga_gain", "dfe_tap1", "slicer_ofst", "eye_vref_code",
        "eye_phase_code", "adapt_hold",
        "pll_locked", "cdr_locked", "sig_detect",
        "deser_rstn",
        "en_afe_bias", "en_pll", "en_rx_fe", "en_cdr",
    ]
    assert len(regs) == 15
    assert [r["offset"] for r in regs] == [4 * i for i in range(15)]
    ro = {r["signal"] for r in regs if r["access"] == "RO"}
    assert ro == {"pll_locked", "cdr_locked", "sig_detect"}
    assert if_c_name(iface["name"]) == "serdes_rx_if"
    # ...and the deterministic map is embedded in the codegen prompt.
    prompt = fw_generate_mod.build_fw_generate_prompt("ser-des", iface, "serdes_rx_if", regs)
    assert "SERDES_RX_IF_CTLE_PEAK" in prompt and "0x0038" in prompt
    assert "15 registers total" in prompt
    assert "serdes_rx_if_regs.h" in prompt and "test_serdes_rx_if.c" in prompt


# ---------------------------------------------------------------------------
# POST /api/fw_generate: stubbed agent + REAL gcc/make honesty check

GOOD_FILES = {
    "t_regs.h": (
        "#ifndef T_REGS_H\n#define T_REGS_H\n"
        "#define T_BASE_ADDR 0x40010000u\n"
        "#define T_TRIM_A_OFFSET 0x0000u\n#define T_TRIM_A_MASK 0xFu\n"
        "#define T_LOCK_A_OFFSET 0x0004u\n#define T_LOCK_A_MASK 0x1u\n"
        "#endif\n"
    ),
    "t_drv.h": (
        "#ifndef T_DRV_H\n#define T_DRV_H\n#include <stdint.h>\n"
        "extern uint32_t fw_reg_read32(uint32_t addr);\n"
        "extern void fw_reg_write32(uint32_t addr, uint32_t v);\n"
        "uint32_t t_get_trim_a(void);\nvoid t_set_trim_a(uint32_t v);\n"
        "#endif\n"
    ),
    "t_drv.c": (
        '#include "t_regs.h"\n#include "t_drv.h"\n'
        "uint32_t t_get_trim_a(void) { return fw_reg_read32(T_BASE_ADDR + T_TRIM_A_OFFSET) & T_TRIM_A_MASK; }\n"
        "void t_set_trim_a(uint32_t v) { fw_reg_write32(T_BASE_ADDR + T_TRIM_A_OFFSET, v & T_TRIM_A_MASK); }\n"
    ),
    "reg_stub.h": (
        "#ifndef REG_STUB_H\n#define REG_STUB_H\n#include <stdint.h>\n"
        "void stub_reset(void);\n#endif\n"
    ),
    "reg_stub.c": (
        '#include "reg_stub.h"\n'
        "static uint32_t regs[64];\n"
        "void stub_reset(void) { for (int i = 0; i < 64; i++) regs[i] = 0; }\n"
        "uint32_t fw_reg_read32(uint32_t addr) { return regs[(addr & 0xFF) >> 2]; }\n"
        "void fw_reg_write32(uint32_t addr, uint32_t v) { regs[(addr & 0xFF) >> 2] = v; }\n"
    ),
    "test_t.c": (
        '#include <stdio.h>\n#include "t_drv.h"\n#include "reg_stub.h"\n'
        "int main(void) {\n"
        "  stub_reset();\n"
        "  t_set_trim_a(5u);\n"
        '  if (t_get_trim_a() != 5u) { printf("FAIL trim round-trip\\n"); return 1; }\n'
        '  printf("ok trim round-trip\\n");\n'
        "  return 0;\n}\n"
    ),
    "Makefile": (
        "test:\n"
        "\tgcc -std=c99 -Wall -Werror -o test_t t_drv.c reg_stub.c test_t.c\n"
        "\t./test_t\n"
    ),
}


def _stub_agent_writing(files: dict[str, str]):
    def stub(prompt, fw_dir, run_dir, timeout_s=0):
        for name, content in files.items():
            (Path(fw_dir) / name).write_text(content)
        return {"ok": True, "reason": "", "cost_usd": 1.23}

    return stub


def test_fw_generate_pass_with_real_gcc_and_make(client, workdir, monkeypatch):
    monkeypatch.setattr(fw_generate_mod, "run_firmware_coder", _stub_agent_writing(GOOD_FILES))
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": minimal_if()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pass", body
    assert body["compile_ok"] is True and body["tests_ok"] is True
    assert body["missing_files"] == []
    assert sorted(body["files"]) == sorted(
        f"firmware/ser-des/{f}" for f in deliverable_files("t")
    )
    assert "ok trim round-trip" in body["test_output"]
    assert body["cost_usd"] == 1.23
    # Artifacts on disk where the charter says.
    fw_dir = workdir / "firmware" / "ser-des"
    for f in deliverable_files("t"):
        assert (fw_dir / f).is_file(), f
    # Run bookkeeping round-trips (Token_Optimizer visibility).
    run_dir = workdir / "fw_runs" / body["run_id"]
    status = json.loads((run_dir / "status.json").read_text())
    assert status["status"] == "pass" and status["kind"] == "fw_generate"
    assert status["cost_usd"] == 1.23 and status["compile_ok"] is True
    spec = json.loads((run_dir / "spec.json").read_text())
    assert [reg["signal"] for reg in spec["register_map"]] == ["trim_a", "lock_a"]
    assert (run_dir / "prompt.txt").exists() and (run_dir / "verify.log").exists()


def test_fw_generate_honesty_check_fails_broken_compile(client, workdir, monkeypatch):
    """The deliberately-broken fixture: an unused variable that -Wall -Werror
    must reject. The agent 'claims' success (ok=True) - the server-side
    re-run must catch it and report fail."""
    broken = dict(GOOD_FILES)
    broken["t_drv.c"] = GOOD_FILES["t_drv.c"] + (
        "int t_broken(void) { int unused_leftover; return 0; }\n"
    )
    monkeypatch.setattr(fw_generate_mod, "run_firmware_coder", _stub_agent_writing(broken))
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": minimal_if()})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "fail"
    assert body["compile_ok"] is False and body["tests_ok"] is False
    assert "compile failed" in body["reason"]
    assert "unused_leftover" in body["test_output"]  # the log tail names the offender
    # Artifacts are kept on failure.
    assert (workdir / "firmware" / "ser-des" / "t_drv.c").is_file()
    status = json.loads(
        (workdir / "fw_runs" / body["run_id"] / "status.json").read_text()
    )
    assert status["status"] == "fail"


def test_fw_generate_honesty_check_fails_failing_test(client, workdir, monkeypatch):
    broken = dict(GOOD_FILES)
    broken["test_t.c"] = (
        '#include <stdio.h>\nint main(void) { printf("FAIL forced\\n"); return 1; }\n'
    )
    monkeypatch.setattr(fw_generate_mod, "run_firmware_coder", _stub_agent_writing(broken))
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": minimal_if()})
    body = r.json()
    assert body["status"] == "fail"
    assert body["compile_ok"] is True and body["tests_ok"] is False
    assert "unit tests failed" in body["reason"]


def test_fw_generate_missing_deliverables_fail(client, workdir, monkeypatch):
    partial = {k: v for k, v in GOOD_FILES.items() if k != "Makefile"}
    monkeypatch.setattr(fw_generate_mod, "run_firmware_coder", _stub_agent_writing(partial))
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": minimal_if()})
    body = r.json()
    assert body["status"] == "fail"
    assert body["missing_files"] == ["Makefile"]
    assert "missing on disk" in body["reason"]


def test_fw_generate_agent_failure_reported(client, workdir, monkeypatch):
    monkeypatch.setattr(
        fw_generate_mod, "run_firmware_coder",
        lambda *a, **k: {"ok": False, "reason": "claude CLI exited with code 1", "cost_usd": None},
    )
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": minimal_if()})
    body = r.json()
    assert body["status"] == "fail" and "exited with code 1" in body["reason"]


def test_fw_generate_422s(client, workdir):
    # unknown phy type (also a path-traversal guard for firmware/<phy_type>/)
    r = client.post("/api/fw_generate", json={"phy_type": "../evil", "interface": minimal_if()})
    assert r.status_code == 422
    # interface with hard validation errors
    broken = minimal_if()
    broken["signals"][0]["direction"] = "sideways"
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": broken})
    assert r.status_code == 422
    assert "unknown direction" in r.json()["detail"]
    # no CSR-reachable signals at all
    empty = minimal_if()
    empty["signals"] = [s for s in empty["signals"] if s["group"] in ("clock", "data")]
    r = client.post("/api/fw_generate", json={"phy_type": "ser-des", "interface": empty})
    assert r.status_code == 422
    assert "no CSR-reachable signals" in r.json()["detail"]
