"""Shared-secret auth gate for the tool's HTTP API (remote-access hardening).

This is deliberately NOT a user-account system - a single shared token (see
settings.auth_token()) is required on every /api/* request, via one of:
  - the `X-Auth-Token` header (what the frontend sends on every request once
    it has a token - see frontend/src/api.js),
  - a `?token=...` query param (convenience for the very first page load;
    main.py's middleware turns a VALID query token into an httpOnly cookie
    so the browser never has to keep re-sending it in the URL), or
  - that cookie itself.

Loopback callers (127.0.0.1/::1) skip the check entirely when
settings.loopback_exempt() is on (the default), so the existing local
workflow keeps working with zero setup. "testclient" is included in the
loopback set purely because it is Starlette's TestClient's synthetic
`request.client.host` for in-process ASGI calls that never touch a real
socket - every existing pytest suite in this repo drives the app exactly
that way (`TestClient(main.app)` with no client= override), and a real
network peer can never present that literal string as its address, so
trusting it costs nothing in a real deployment.
"""

from __future__ import annotations

import secrets
from typing import Optional

from starlette.requests import Request

import settings as settings_mod

COOKIE_NAME = "aspt_token"
HEADER_NAME = "x-auth-token"

# See the module docstring for why "testclient" belongs here.
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "testclient"}


def _const_time_eq(candidate: Optional[str], token: str) -> bool:
    return bool(candidate) and secrets.compare_digest(candidate, token)


def is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else None
    return host in LOOPBACK_HOSTS


def query_token(request: Request) -> Optional[str]:
    return request.query_params.get("token")


def has_valid_query_token(request: Request, token: str) -> bool:
    return _const_time_eq(query_token(request), token)


def is_authorized(request: Request, token: str) -> bool:
    """True if this request may proceed: loopback-exempt (if enabled and the
    peer is loopback), a matching X-Auth-Token header, a matching cookie, or
    a matching ?token= query param."""
    if settings_mod.loopback_exempt() and is_loopback_request(request):
        return True
    if _const_time_eq(request.headers.get(HEADER_NAME), token):
        return True
    if _const_time_eq(request.cookies.get(COOKIE_NAME), token):
        return True
    if has_valid_query_token(request, token):
        return True
    return False
