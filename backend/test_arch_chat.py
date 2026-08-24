"""
Unit/integration tests for the architecture-discussion chat feature
(backend/arch_chat.py, backend/architectures.py, /api/arch_chat and
/api/architectures endpoints). All claude invocations are STUBBED - running
this suite costs nothing.

Run:  cd backend && .venv/bin/python3 -m pytest test_arch_chat.py -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import arch_chat
from arch_chat import (
    normalize_architecture,
    parse_reply_and_patch,
    validate_patch,
    validate_session_id,
)


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """Point the tool's settings at a throwaway working dir (the documented
    ANALOG_SPEC_TOOL_SETTINGS override - settings re-reads on every call, so
    no reimport needed)."""
    settings_file = tmp_path / "settings.json"
    wd = tmp_path / "workdir"
    settings_file.write_text(json.dumps({"working_dir": str(wd)}))
    monkeypatch.setenv("ANALOG_SPEC_TOOL_SETTINGS", str(settings_file))
    return wd


@pytest.fixture()
def client(workdir):
    import main

    return TestClient(main.app)


ARCH = {
    "blocks": [
        {"id": "rx", "label": "RX CTLE", "topology": "ctle", "col": 0, "row": 0},
        {"id": "sampler", "label": "Sampler", "topology": "sense_amp_slicer", "col": 1, "row": 0},
        {"id": "pad", "label": "RX PAD", "topology": None, "kind": "pad", "col": 0, "row": 1},
    ],
    "edges": [
        {"from": "pad", "to": "rx"},
        {"from": "rx", "to": "sampler"},
    ],
}


def norm():
    return normalize_architecture(ARCH)


# ---------------------------------------------------------------------------
# validate_patch


def test_valid_ops_pass():
    ops, valid = validate_patch(
        {
            "ops": [
                {"op": "add_block", "id": "vga", "label": "VGA", "topology": "vga"},
                {"op": "remove_connection", "from": "rx", "to": "sampler"},
                {"op": "add_connection", "from": "rx", "to": "vga"},
                {"op": "add_connection", "from": "vga", "to": "sampler"},
                {"op": "rename_block", "id": "rx", "label": "RX CTLE (stage 1)"},
                {"op": "set_topology", "id": "sampler", "topology": "comparator"},
            ]
        },
        norm(),
    )
    assert valid is True
    assert all("error" not in op for op in ops)


def test_bad_topology_value_rejected():
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "x", "label": "X", "topology": "flux_capacitor"}]},
        norm(),
    )
    assert valid is False
    assert "unknown topology" in ops[0]["error"]


def test_set_topology_bad_value_rejected():
    ops, valid = validate_patch(
        {"ops": [{"op": "set_topology", "id": "rx", "topology": "not_a_thing"}]}, norm()
    )
    assert valid is False
    assert "unknown topology" in ops[0]["error"]


def test_null_topology_allowed():
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "pd", "label": "Photodiode", "topology": None}]},
        norm(),
    )
    assert valid is True


def test_dangling_connection_rejected():
    ops, valid = validate_patch(
        {"ops": [{"op": "add_connection", "from": "rx", "to": "nonexistent"}]}, norm()
    )
    assert valid is False
    assert "no block with id 'nonexistent'" in ops[0]["error"]


def test_connection_to_removed_block_rejected():
    ops, valid = validate_patch(
        {
            "ops": [
                {"op": "remove_block", "id": "sampler"},
                {"op": "add_connection", "from": "rx", "to": "sampler"},
            ]
        },
        norm(),
    )
    assert valid is False
    assert "error" not in ops[0]
    assert "no block with id 'sampler'" in ops[1]["error"]


def test_remove_block_removes_its_edges():
    # rx's edges disappear with it, so removing pad->rx afterwards is dangling.
    ops, valid = validate_patch(
        {
            "ops": [
                {"op": "remove_block", "id": "rx"},
                {"op": "remove_connection", "from": "pad", "to": "rx"},
            ]
        },
        norm(),
    )
    assert valid is False
    assert "no connection" in ops[1]["error"]


def test_duplicate_block_id_rejected():
    ops, valid = validate_patch(
        {"ops": [{"op": "add_block", "id": "rx", "label": "dup", "topology": "vga"}]}, norm()
    )
    assert valid is False
    assert "already exists" in ops[0]["error"]


def test_duplicate_and_unknown_ops_rejected():
    ops, valid = validate_patch(
        {
            "ops": [
                {"op": "add_connection", "from": "rx", "to": "sampler"},  # already exists
                {"op": "teleport_block", "id": "rx"},
            ]
        },
        norm(),
    )
    assert valid is False
    assert "already exists" in ops[0]["error"]
    assert "unknown op" in ops[1]["error"]


def test_patch_not_an_ops_object():
    ops, valid = validate_patch(["not", "a", "dict"], norm())
    assert valid is False
    assert ops[0]["error"]


# ---------------------------------------------------------------------------
# parse_reply_and_patch


def test_parse_reply_with_patch():
    text = (
        "You should add a VGA.\n\n"
        '```json\n{"ops": [{"op": "add_block", "id": "vga", "label": "VGA", "topology": "vga"}]}\n```'
    )
    reply, patch = parse_reply_and_patch(text)
    assert reply == "You should add a VGA."
    assert patch == {"ops": [{"op": "add_block", "id": "vga", "label": "VGA", "topology": "vga"}]}


def test_parse_reply_plain_text():
    reply, patch = parse_reply_and_patch("Just an answer, no changes needed.")
    assert reply == "Just an answer, no changes needed."
    assert patch is None


def test_parse_malformed_json_never_fails_chat():
    text = 'Here you go.\n```json\n{"ops": [{{{ broken\n```'
    reply, patch = parse_reply_and_patch(text)
    assert patch is None
    assert "Here you go." in reply  # full text kept


def test_parse_json_block_without_ops_is_not_a_patch():
    text = 'Some data:\n```json\n{"foo": 1}\n```\nthe end'
    reply, patch = parse_reply_and_patch(text)
    assert patch is None
    assert "the end" in reply


# ---------------------------------------------------------------------------
# normalize_architecture / session ids


def test_connections_alias_accepted():
    arch = normalize_architecture(
        {"blocks": [{"id": "a"}, {"id": "b"}], "connections": [{"from": "a", "to": "b"}]}
    )
    assert arch["edges"] == [{"from": "a", "to": "b"}]


def test_bad_architecture_rejected():
    with pytest.raises(ValueError):
        normalize_architecture({"blocks": [{"label": "no id"}]})
    with pytest.raises(ValueError):
        normalize_architecture({"blocks": [{"id": "a"}, {"id": "a"}]})


def test_session_id_traversal_rejected():
    with pytest.raises(ValueError):
        validate_session_id("../../etc/passwd")
    with pytest.raises(ValueError):
        validate_session_id("")
    assert validate_session_id("chat-2026_08-21A") == "chat-2026_08-21A"


# ---------------------------------------------------------------------------
# POST /api/arch_chat with a stubbed consultant


def _stub_result(raw_text):
    return arch_chat.ArchChatResult(
        status="success", raw_text=raw_text, session_id="cli-sess", cost_usd=0.01, duration_ms=1
    )


def test_chat_two_messages_persist_transcript(client, workdir, monkeypatch):
    replies = iter(
        [
            "First answer, informational only.",
            'Second answer with a patch.\n```json\n{"ops": [{"op": "add_block", "id": "vga", '
            '"label": "VGA", "topology": "vga"}, {"op": "add_connection", "from": "rx", "to": "vga"}]}\n```',
        ]
    )
    prompts = []

    def stub(prompt, log_dir, log_stem, timeout_s=0):
        prompts.append(prompt)
        return _stub_result(next(replies))

    monkeypatch.setattr(arch_chat, "run_arch_consultant", stub)

    r1 = client.post(
        "/api/arch_chat",
        json={"session_id": "sess1", "message": "Why a CTLE here?", "architecture": ARCH},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["reply"] == "First answer, informational only."
    assert body1["patch"] is None and body1["patch_valid"] is True

    r2 = client.post(
        "/api/arch_chat",
        json={"session_id": "sess1", "message": "Add a VGA after the CTLE.", "architecture": ARCH},
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["patch_valid"] is True
    assert [op["op"] for op in body2["patch"]["ops"]] == ["add_block", "add_connection"]

    # Second prompt carried the first exchange as history.
    assert "Why a CTLE here?" in prompts[1]
    assert "First answer, informational only." in prompts[1]

    # Transcript persisted on disk with all four turns.
    tfile = workdir / "arch_chats" / "sess1.json"
    assert tfile.exists()
    data = json.loads(tfile.read_text())
    assert [t["role"] for t in data["turns"]] == ["user", "assistant", "user", "assistant"]

    # And served back via GET.
    r3 = client.get("/api/arch_chat/sess1")
    assert r3.status_code == 200
    assert len(r3.json()["turns"]) == 4


def test_chat_invalid_patch_reported_not_fatal(client, workdir, monkeypatch):
    monkeypatch.setattr(
        arch_chat,
        "run_arch_consultant",
        lambda *a, **k: _stub_result(
            'Sure.\n```json\n{"ops": [{"op": "set_topology", "id": "ghost", "topology": "vga"}]}\n```'
        ),
    )
    r = client.post(
        "/api/arch_chat",
        json={"session_id": "sess2", "message": "change ghost", "architecture": ARCH},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == "Sure."
    assert body["patch_valid"] is False
    assert body["patch_errors"][0]["index"] == 0
    assert "no block with id 'ghost'" in body["patch"]["ops"][0]["error"]


def test_chat_malformed_model_json_returns_text(client, workdir, monkeypatch):
    monkeypatch.setattr(
        arch_chat,
        "run_arch_consultant",
        lambda *a, **k: _stub_result("Answer.\n```json\n{not json at all\n```"),
    )
    r = client.post(
        "/api/arch_chat",
        json={"session_id": "sess3", "message": "hello", "architecture": ARCH},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patch"] is None
    assert "Answer." in body["reply"]


def test_chat_consultant_failure_is_502(client, workdir, monkeypatch):
    monkeypatch.setattr(
        arch_chat,
        "run_arch_consultant",
        lambda *a, **k: arch_chat.ArchChatResult(status="failed", reason="timed out"),
    )
    r = client.post(
        "/api/arch_chat",
        json={"session_id": "sess4", "message": "hello", "architecture": ARCH},
    )
    assert r.status_code == 502
    assert "timed out" in r.json()["detail"]
    # No fake exchange recorded.
    assert client.get("/api/arch_chat/sess4").json()["turns"] == []


def test_chat_bad_session_id_422(client, workdir):
    r = client.post(
        "/api/arch_chat",
        json={"session_id": "../evil", "message": "hi", "architecture": ARCH},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# /api/architectures save/list round trip


def test_architectures_save_list_roundtrip(client, workdir):
    r0 = client.get("/api/architectures")
    assert r0.status_code == 200 and r0.json()["architectures"] == []

    r1 = client.post(
        "/api/architectures",
        json={"name": "my-serdes-v2", "phy_type": "ser-des", "label": "SerDes + VGA", "architecture": ARCH},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["name"] == "my-serdes-v2"

    r2 = client.get("/api/architectures")
    entries = r2.json()["architectures"]
    assert len(entries) == 1
    e = entries[0]
    assert e["label"] == "SerDes + VGA" and e["phy_type"] == "ser-des"
    assert {b["id"] for b in e["architecture"]["blocks"]} == {"rx", "sampler", "pad"}
    assert (workdir / "architectures" / "my-serdes-v2.json").exists()

    # Overwrite with the same name updates in place (no duplicate).
    r3 = client.post(
        "/api/architectures",
        json={"name": "my-serdes-v2", "architecture": ARCH},
    )
    assert r3.status_code == 200 and "updated_at" in r3.json()
    assert len(client.get("/api/architectures").json()["architectures"]) == 1


def test_architectures_bad_name_and_shape_422(client, workdir):
    r = client.post("/api/architectures", json={"name": "bad/name", "architecture": ARCH})
    assert r.status_code == 422
    r = client.post(
        "/api/architectures", json={"name": "ok-name", "architecture": {"blocks": "nope"}}
    )
    assert r.status_code == 422
