"""
Auth + proxy-correctness tests for the interactive-xschem WebSocket route
(/api/xschem/sessions/{id}/ws - see backend/xschem_session.py and the route
in backend/main.py). This is the single most important test in this feature:
a VNC bridge is a remote desktop into the user's machine, so an
unauthenticated peer must never be able to complete the WebSocket handshake,
regardless of whether the session_id it names exists.

Same throwaway-settings-file / REMOTE_PEER / loopback_client conventions as
tests/test_auth.py - see that file's docstring for why "testclient" is safe
to trust as a loopback host in every OTHER suite, and why these tests
explicitly override TestClient's `client=` to exercise the real-remote-caller
code path.

Run:  cd backend && .venv/bin/python3 -m pytest tests/test_xschem_ws_auth.py -q
"""

from __future__ import annotations

import json
import socket
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

REMOTE_PEER = ("203.0.113.7", 54322)  # TEST-NET-3 (RFC 5737) - never a real caller


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
def token(app_module):
    import settings as settings_mod

    return settings_mod.auth_token()


@pytest.fixture()
def remote_client(app_module):
    return TestClient(app_module.app, client=REMOTE_PEER)


@pytest.fixture()
def loopback_client(app_module):
    return TestClient(app_module.app, client=("127.0.0.1", 12346))


def test_unauthenticated_peer_cannot_reach_the_websocket_at_all(remote_client, token):
    """No header, no cookie, no ?token= - the connection must be refused
    before ANY session lookup happens (the session_id here doesn't even
    exist), proving auth is checked first and unconditionally."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with remote_client.websocket_connect("/api/xschem/sessions/nonexistent-session/ws"):
            pass
    assert exc_info.value.code == 4401


def test_wrong_token_cannot_reach_the_websocket(remote_client, token):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with remote_client.websocket_connect(
            "/api/xschem/sessions/nonexistent-session/ws",
            headers={"X-Auth-Token": "not-the-token"},
        ):
            pass
    assert exc_info.value.code == 4401


def test_correct_query_token_passes_auth_but_session_still_not_found(remote_client, token):
    """Proves auth is checked BEFORE the session lookup, not instead of it:
    a correctly authenticated peer asking for a session that doesn't exist
    gets a DIFFERENT close code (4404, "session not found"), never 4401."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with remote_client.websocket_connect(f"/api/xschem/sessions/nonexistent-session/ws?token={token}"):
            pass
    assert exc_info.value.code == 4404


def test_loopback_caller_exempt_same_as_every_other_api_route(loopback_client, token):
    """Loopback exemption (settings.loopback_exempt(), default True) applies
    to the WebSocket route exactly like every /api/* HTTP route - proven by
    getting the SESSION-NOT-FOUND code, not the auth-rejection one, with zero
    credentials supplied."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with loopback_client.websocket_connect("/api/xschem/sessions/nonexistent-session/ws"):
            pass
    assert exc_info.value.code == 4404


def test_loopback_exemption_off_still_blocks_unauthenticated_ws(loopback_client, token, workdir):
    import settings as settings_mod

    settings_mod.set_loopback_exempt(False)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with loopback_client.websocket_connect("/api/xschem/sessions/nonexistent-session/ws"):
                pass
        assert exc_info.value.code == 4401
    finally:
        settings_mod.set_loopback_exempt(True)


class _EchoServer:
    """A tiny loopback TCP echo server standing in for x11vnc's RFB socket -
    proves the WebSocket route's byte-proxy loop actually carries bytes both
    directions end to end, independent of any real VNC/xschem details."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(5.0)
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.settimeout(5.0)
        try:
            while not self._stop:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self._stop = True
        self.sock.close()


def test_authenticated_websocket_proxies_bytes_both_ways_to_the_session_endpoint(
    remote_client, token, app_module, monkeypatch
):
    """End-to-end proxy correctness: registers a fake session backed by a
    real loopback TCP echo server (standing in for x11vnc), connects through
    the authenticated WebSocket route, sends bytes, and checks the SAME bytes
    come back - proving the route really does pipe the two sides together
    rather than just accepting the handshake."""
    echo = _EchoServer()
    try:
        monkeypatch.setattr(
            app_module.xschem_session_mod,
            "get_vnc_endpoint",
            lambda session_id: ("127.0.0.1", echo.port) if session_id == "fake-session" else None,
        )
        touched = {"connect": 0, "disconnect": 0}
        monkeypatch.setattr(
            app_module.xschem_session_mod, "touch_connect",
            lambda sid: touched.__setitem__("connect", touched["connect"] + 1),
        )
        monkeypatch.setattr(
            app_module.xschem_session_mod, "touch_disconnect",
            lambda sid: touched.__setitem__("disconnect", touched["disconnect"] + 1),
        )
        with remote_client.websocket_connect(f"/api/xschem/sessions/fake-session/ws?token={token}") as ws:
            ws.send_bytes(b"RFB 003.008\n")
            echoed = ws.receive_bytes()
            assert echoed == b"RFB 003.008\n"
        assert touched["connect"] == 1
        assert touched["disconnect"] == 1
    finally:
        echo.close()
