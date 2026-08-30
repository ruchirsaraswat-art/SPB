"""
Unit tests for the shared-secret auth gate (backend/auth.py + main.py's
`_auth_gate` middleware, remote-access hardening). No claude invocations are
exercised - this suite costs nothing.

Every existing pytest suite in this repo (test_arch_chat.py, test_rtl.py,
test_digital_if.py, test_firmware.py, tests/test_deliverables.py) drives the
app via a plain `TestClient(main.app)`, whose synthetic peer address is
"testclient" (Starlette's default) - included in auth.LOOPBACK_HOSTS for
exactly that reason, so those suites stay green untouched. To actually
exercise the "real remote caller" paths here, these tests explicitly pass a
non-loopback `client=` address to TestClient.

Run:  cd backend && .venv/bin/python3 -m pytest tests/test_auth.py -q
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

REMOTE_PEER = ("203.0.113.5", 54321)  # TEST-NET-3 (RFC 5737) - never a real caller


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    """Same throwaway-settings-file pattern as every other suite here - the
    auto-generated token this exercises is written to THIS tmp settings
    file, never the user's real one."""
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
def token(app_module):
    import settings as settings_mod

    return settings_mod.auth_token()


@pytest.fixture()
def remote_client(app_module):
    """A client whose request.client.host is a real (non-loopback,
    non-"testclient") address - exercises the gate as an actual remote
    caller would hit it."""
    return TestClient(app_module.app, client=REMOTE_PEER)


@pytest.fixture()
def loopback_client(app_module):
    return TestClient(app_module.app, client=("127.0.0.1", 12345))


def test_no_token_is_rejected(remote_client, token):
    res = remote_client.get("/api/topologies")
    assert res.status_code == 401
    assert "detail" in res.json()


def test_wrong_token_is_rejected(remote_client, token):
    res = remote_client.get("/api/topologies", headers={"X-Auth-Token": "not-the-token"})
    assert res.status_code == 401


def test_correct_header_authenticates(remote_client, token):
    res = remote_client.get("/api/topologies", headers={"X-Auth-Token": token})
    assert res.status_code == 200


def test_query_token_authenticates_and_sets_cookie(remote_client, token):
    res = remote_client.get(f"/api/topologies?token={token}")
    assert res.status_code == 200
    assert "aspt_token" in res.cookies
    assert res.cookies["aspt_token"] == token


def test_wrong_query_token_does_not_authenticate_or_set_cookie(remote_client, token):
    res = remote_client.get("/api/topologies?token=nope")
    assert res.status_code == 401
    assert "aspt_token" not in res.cookies


def test_cookie_from_prior_query_token_authenticates_next_request(remote_client, token):
    first = remote_client.get(f"/api/topologies?token={token}")
    assert first.status_code == 200
    # remote_client persists cookies across requests on the same instance -
    # a second call with NO header/query token at all should now succeed
    # purely off the cookie set by the first response.
    second = remote_client.get("/api/topologies")
    assert second.status_code == 200


def test_loopback_is_exempt_by_default(loopback_client, token):
    res = loopback_client.get("/api/topologies")
    assert res.status_code == 200


def test_loopback_exemption_can_be_turned_off(loopback_client, remote_client, token, workdir):
    import settings as settings_mod

    settings_mod.set_loopback_exempt(False)
    res = loopback_client.get("/api/topologies")
    assert res.status_code == 401
    # A correct header still works even with the exemption off.
    res2 = loopback_client.get("/api/topologies", headers={"X-Auth-Token": token})
    assert res2.status_code == 200


def test_401_is_json_not_a_redirect(remote_client, token):
    res = remote_client.get("/api/topologies", follow_redirects=False)
    assert res.status_code == 401
    assert res.headers.get("content-type", "").startswith("application/json")


def test_env_token_override_wins_over_persisted_token(remote_client, token, monkeypatch):
    monkeypatch.setenv("ANALOG_SPEC_TOOL_TOKEN", "env-override-token")
    # The persisted token from the `token` fixture no longer works...
    res = remote_client.get("/api/topologies", headers={"X-Auth-Token": token})
    assert res.status_code == 401
    # ...only the env-provided one does.
    res2 = remote_client.get("/api/topologies", headers={"X-Auth-Token": "env-override-token"})
    assert res2.status_code == 200
