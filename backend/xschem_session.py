"""
Interactive xschem-over-VNC session manager (browser-reachable "Edit in
xschem", 2026-09 remote-access spec).

Why this exists: backend/schematics.py's launch_xschem() spawns interactive
xschem on the BACKEND MACHINE's own X display - great when you're sitting at
that machine, useless over Tailscale from a Mac where nothing renders where
you can see it. This module gives each session its own throwaway X server
instead, and streams its framebuffer to the browser over a WebSocket the
frontend already reaches through the tool's normal (authenticated,
single-origin) HTTP surface.

Architecture, one session:
    Xvfb :N            - a fresh, headless-but-real X11 display, display
                          number N freshly allocated (never :2, the
                          FALLBACK_DISPLAY schematics.py uses for the
                          machine's real desktop - these are independent).
    xschem <sch>        - launched with DISPLAY=:N and cwd = the target
                          directory (a filed library cell dir or a run dir),
                          whose xschemrc resolves sky130 symbols - reuses
                          circuit_builder._write_project_xschemrc exactly
                          like schematics.py's launch_xschem does. NEVER
                          `xschem -x` - that flag is headless PNG export and
                          would defeat the point of an interactive session.
    x11vnc              - screen-scrapes display :N, bound to 127.0.0.1
                          ONLY (-localhost; never 0.0.0.0 - see
                          SECURITY below), on a freshly allocated loopback
                          TCP port, protected by a fresh per-session random
                          password.

SECURITY MODEL (this is the part that must not be got wrong - see the task
spec): the browser NEVER talks to x11vnc directly. backend/main.py exposes
one WebSocket route, /api/xschem/sessions/{id}/ws, which this module's
get_vnc_endpoint() resolves to x11vnc's 127.0.0.1:<port>, and main.py proxies
raw bytes between the browser's WebSocket and that loopback TCP socket
(noVNC's wire format is just the RFB protocol carried unmodified inside
WebSocket binary frames - the proxy never has to understand RFB itself).

Three independent layers, so no single mistake opens the machine to Tailscale
peers who don't have the tool's token:
  1. x11vnc binds 127.0.0.1 only (enforced by the exact argv built in
     _spawn_x11vnc - never configurable to 0.0.0.0 from the API). A remote
     peer cannot reach the VNC port directly, full stop, regardless of the
     auth gate below.
  2. The WebSocket route is NOT covered by main.py's `@app.middleware("http")`
     auth gate - HTTP middleware never sees "websocket" scope ASGI messages
     in Starlette, only "http" ones. So the route calls
     `auth.is_authorized(websocket, token)` ITSELF, first thing, before
     accepting the handshake at all - same shared-secret check as every
     /api/* route (header, cookie, or ?token= query param; loopback-exempt
     the same way). An unauthenticated peer's handshake is closed (never
     accepted) before this module's proxy loop or x11vnc are touched at all.
  3. Even a caller who reaches the proxy (i.e. already holds the tool's
     shared token) still needs the per-session VNC password this module
     generates fresh in start_session() - defense in depth against another
     local account on the same machine connecting to the loopback port
     directly, bypassing the HTTP auth gate entirely. The password is
     returned to the caller exactly once, in start_session()'s return value,
     over the same authenticated API call that started the session - never
     persisted to disk, never logged.

Lifecycle: explicit start_session()/stop_session(), an idle reaper
(reap_idle_once(), driven by a background thread main.py starts at startup -
see start_background_reaper()) that kills a session nobody's connected to for
IDLE_TIMEOUT_S, a cap on concurrent sessions (MAX_SESSIONS), and
cleanup_orphans_on_startup() (called from main.py's existing startup event,
right alongside _repair_orphaned_channels()) which kills any Xvfb/xschem/
x11vnc left running by a killed backend - found via the session.json files
this module persists under settings.xschem_sessions_root().

Graceful degradation: prereqs_status() checks Xvfb/xschem/x11vnc are all on
PATH and reports exactly what's missing (with the apt-get install line) if
not; start_session() raises PrereqMissing wrapping that status rather than
ever attempting to launch a binary that isn't there.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from circuit_builder import _write_project_xschemrc
from settings import xschem_sessions_root

# --- tunables ----------------------------------------------------------------

# Display numbers reserved for these ephemeral sessions - deliberately
# disjoint from schematics.FALLBACK_DISPLAY (":2", the machine's real desktop
# session) so a browser session can never collide with (or accidentally
# piggyback on) the interactive desktop.
DISPLAY_MIN = 50
DISPLAY_MAX = 99

# Loopback TCP ports handed to x11vnc (freshly probed free at allocation
# time, not just this range - the range is only where the search starts).
VNC_PORT_MIN = 15900
VNC_PORT_MAX = 15999

MAX_SESSIONS = 3
IDLE_TIMEOUT_S = 15 * 60  # no connected client for 15 minutes -> reaped
REAP_INTERVAL_S = 30

X11_SOCKET_DIR = Path("/tmp/.X11-unix")

# --- display resolution (2026-09 resize feature) ------------------------------
#
# Xvfb's RANDR implementation on this machine is a stub - `xrandr --newmode`/
# `--fb` cannot resize a running display (verified empirically). So unlike a
# real X server, the ONLY way to change a session's drawing area is to kill
# its Xvfb and start a new, bigger one - see restart_session() below. That is
# a genuinely destructive operation (kills xschem, so any unsaved on-canvas
# work is lost) and callers (backend/main.py's routes) must never do it
# silently - the frontend gets explicit user confirmation first.
#
# A short, fixed preset list (rather than free-form WxH) keeps this a "pick a
# bigger box" decision, not a way to wedge Xvfb with a pathological size.
ALLOWED_RESOLUTIONS = ("1280x800", "1600x1000", "1920x1200", "2560x1600")
# More generous than the original hardcoded 1280x800 default - safe (Xvfb's
# memory cost for a bigger virtual framebuffer is negligible) and gives
# xschem noticeably more drawing area out of the box.
DEFAULT_RESOLUTION = "1600x1000"


def validate_resolution(resolution: str) -> tuple[int, int]:
    """Parse+validate a "WxH" resolution string, returning (width, height).
    Raises ValueError (not LaunchFailed - this is a client input-validation
    error, not a launch-time failure) if it isn't one of ALLOWED_RESOLUTIONS."""
    if resolution not in ALLOWED_RESOLUTIONS:
        raise ValueError(
            f"invalid resolution {resolution!r} - choose one of: {', '.join(ALLOWED_RESOLUTIONS)}"
        )
    w_str, h_str = resolution.split("x")
    return int(w_str), int(h_str)

REQUIRED_BINARIES = ("Xvfb", "xschem", "x11vnc")


class PrereqMissing(RuntimeError):
    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__(status.get("message", "missing prerequisite"))


class TooManySessions(RuntimeError):
    pass


class LaunchFailed(RuntimeError):
    pass


# --- prerequisite check (graceful degradation) -------------------------------


def prereqs_status() -> dict[str, Any]:
    """Whether every binary this feature needs is on PATH, checked at call
    time (not cached - the user may install x11vnc while the backend is
    running). Missing binaries are reported by their exact apt package name
    so the frontend can show a copy-pasteable install line."""
    missing = [name for name in REQUIRED_BINARIES if shutil.which(name) is None]
    if missing:
        pkgs = " ".join(sorted(m.lower() for m in missing))
        return {
            "available": False,
            "missing": missing,
            "message": (
                f"Interactive xschem-in-browser needs {', '.join(missing)} installed on "
                f"this machine. Install with: sudo apt install {pkgs}"
            ),
        }
    return {"available": True, "missing": []}


# --- session state ------------------------------------------------------------


@dataclass
class SessionInfo:
    session_id: str
    key: str  # str(sch_path.resolve()) - one live session per schematic file
    target_dir: str
    sch_path: str
    label: str
    display_num: int
    vnc_port: int
    vnc_password: str
    resolution: str
    created_at: str
    last_active_at: str
    xvfb_pid: int
    xschem_pid: int
    x11vnc_pid: int
    clients: int = 0


_SESSIONS: dict[str, SessionInfo] = {}
_PROCS: dict[str, dict[str, subprocess.Popen]] = {}
_lock = threading.RLock()
_reaper_thread: Optional[threading.Thread] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _session_dir(session_id: str) -> Path:
    return xschem_sessions_root() / session_id


def _persist(info: SessionInfo) -> None:
    d = _session_dir(info.session_id)
    d.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in info.__dict__.items() if k != "vnc_password"}
    (d / "session.json").write_text(json.dumps(payload, indent=2))


def _public(info: SessionInfo) -> dict[str, Any]:
    procs = _PROCS.get(info.session_id) or {}
    alive = bool(procs) and all(p.poll() is None for p in procs.values())
    return {
        "session_id": info.session_id,
        "label": info.label,
        "target_dir": info.target_dir,
        "sch_path": info.sch_path,
        "display": f":{info.display_num}",
        "resolution": info.resolution,
        "created_at": info.created_at,
        "last_active_at": info.last_active_at,
        "clients": info.clients,
        "alive": alive,
    }


# --- allocation ---------------------------------------------------------------


def _alloc_display() -> int:
    used = {info.display_num for info in _SESSIONS.values()}
    for n in range(DISPLAY_MIN, DISPLAY_MAX + 1):
        if n in used:
            continue
        if (X11_SOCKET_DIR / f"X{n}").exists():
            continue
        return n
    raise LaunchFailed(
        f"no free X display slot in :{DISPLAY_MIN}-:{DISPLAY_MAX} - stop an existing "
        "xschem session before starting another"
    )


def _alloc_port() -> int:
    used = {info.vnc_port for info in _SESSIONS.values()}
    for p in range(VNC_PORT_MIN, VNC_PORT_MAX + 1):
        if p in used:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            continue
        finally:
            s.close()
        return p
    raise LaunchFailed(
        f"no free loopback port in {VNC_PORT_MIN}-{VNC_PORT_MAX} for x11vnc"
    )


# --- process spawning (each a thin subprocess.Popen wrapper so tests can ----
# --- monkeypatch subprocess.Popen and drive the whole lifecycle without a --
# --- real X server) -----------------------------------------------------------


def _wait_for(predicate, timeout_s: float, interval_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _spawn_xvfb(display_num: int, log_path: Path, resolution: str = DEFAULT_RESOLUTION) -> subprocess.Popen:
    with open(log_path, "w") as logf:
        return subprocess.Popen(
            ["Xvfb", f":{display_num}", "-screen", "0", f"{resolution}x24", "-nolisten", "tcp", "-ac"],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _spawn_xschem(
    sch_path: Path, cwd: Path, display_num: int, log_path: Path, resolution: str = DEFAULT_RESOLUTION
) -> subprocess.Popen:
    env = os.environ.copy()
    env["DISPLAY"] = f":{display_num}"
    w, h = validate_resolution(resolution)
    # xschem's own Tk toplevel does NOT fill the display by default (there is
    # no window manager on these throwaway Xvfb displays to do that for it -
    # it opens at a small fixed 900x600) - `--command` runs a Tcl command
    # after xschem finishes its own startup, so `wm geometry . <W>x<H>+0+0`
    # resizes+repositions xschem's actual window to exactly fill whatever
    # display size Xvfb was given. Verified empirically (2026-09): without
    # this, a bigger Xvfb buys nothing - xschem just sits in the corner of a
    # bigger black canvas.
    with open(log_path, "w") as logf:
        return subprocess.Popen(
            ["xschem", "--command", f"wm geometry . {w}x{h}+0+0", str(sch_path)],
            cwd=str(cwd),
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _spawn_x11vnc(display_num: int, port: int, password: str, log_path: Path) -> subprocess.Popen:
    # x11vnc's own startup check for "am I on a Wayland session" only looks
    # at $WAYLAND_DISPLAY/$XDG_SESSION_TYPE in ITS environment - it does not
    # actually probe whether the X11 `-display` it was TOLD to use (our
    # freshly allocated Xvfb, always real X11) is a Wayland one. On a
    # machine whose real desktop session happens to be Wayland (true of this
    # dev box), those two env vars are inherited from the backend process
    # and make x11vnc refuse to start with "Wayland sessions ... Exiting"
    # even though :N is a plain Xvfb it would work with fine. Strip them
    # from JUST this child's environment - irrelevant/misleading here
    # regardless, since this process only ever touches the Xvfb display we
    # allocated for it.
    env = os.environ.copy()
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("XDG_SESSION_TYPE", None)
    with open(log_path, "w") as logf:
        return subprocess.Popen(
            [
                "x11vnc",
                "-display", f":{display_num}",
                "-rfbport", str(port),
                # This x11vnc build has IPv6 listening ("-6") compiled in as
                # the default, and - empirically verified, 2026-09 - its
                # IPv6 socket does NOT inherit -rfbport's value on its own
                # (nor is it disabled by -no6/-noipv6 in this build): it
                # falls back to plain TCP6 5900 regardless. Still
                # loopback-only (::1, never a real interface, so not a
                # remote-exposure hole) but a stray FIXED port would make a
                # second concurrent session's x11vnc fail to bind it - so
                # pin the IPv6 listener to the SAME port we allocated for
                # IPv4 explicitly, rather than trying to suppress IPv6
                # listening (which this build doesn't reliably honor).
                "-rfbportv6", str(port),
                "-localhost",  # NEVER drop this - the only thing standing between
                                # this VNC server and every host on the tailnet.
                "-passwd", password,
                "-shared",
                "-forever",
                "-noxdamage",
                "-quiet",
            ],
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _terminate(proc: subprocess.Popen, grace_s: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    if not _wait_for(lambda: proc.poll() is not None, grace_s):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _kill_pid(pid: int) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# --- public API ----------------------------------------------------------------


def start_session(
    cwd: Path, sch_path: Path, label: str, resolution: str = DEFAULT_RESOLUTION
) -> dict[str, Any]:
    """Start (or reuse) an interactive xschem-over-VNC session for `sch_path`
    (cwd = its project directory, same convention as
    schematics.launch_xschem). Returns a dict including the one-time
    `vnc_password` the caller hands to the frontend's noVNC client. Raises
    ValueError for an invalid `resolution` (caller turns that into a 422),
    PrereqMissing / TooManySessions / LaunchFailed - callers turn those into
    409/429/500 respectively, never a crash.

    NOTE on reuse: if a session for this exact `sch_path` is already running,
    it is reused AS-IS regardless of `resolution` - this function never
    resizes a live session out from under a caller who merely asked to open
    it again (that would silently kill unsaved xschem work). Changing an
    existing session's resolution is a deliberate, explicit action - see
    restart_session()."""
    validate_resolution(resolution)  # ValueError first - cheap, no I/O, no side effects yet
    status = prereqs_status()
    if not status["available"]:
        raise PrereqMissing(status)
    if not sch_path.is_file():
        raise LaunchFailed(f"schematic file {sch_path} does not exist")

    key = str(sch_path.resolve())
    with _lock:
        for info in _SESSIONS.values():
            if info.key == key:
                procs = _PROCS.get(info.session_id) or {}
                if procs and all(p.poll() is None for p in procs.values()):
                    info.last_active_at = _now()
                    return {**_public(info), "vnc_password": info.vnc_password, "reused": True}
                # Stale entry (a process died on its own) - fall through and
                # replace it below rather than reusing a dead session.
                stop_session(info.session_id)
                break

        if len(_SESSIONS) >= MAX_SESSIONS:
            raise TooManySessions(
                f"{MAX_SESSIONS} interactive xschem sessions are already open - stop one first"
            )

        # ALWAYS (re)write - never trust a pre-existing xschemrc here, unlike
        # schematics.py's local-display launch_xschem which only writes one
        # if missing. A stale xschemrc (e.g. written by an older version of
        # this tool, before a security fix such as the bespice-listener
        # disable below) would otherwise silently keep whatever it already
        # had - and unlike the local-display path, a VNC session is reachable
        # over the network, so xschemrc correctness here is a SECURITY
        # invariant, not just a convenience default.
        _write_project_xschemrc(cwd)

        session_id = uuid.uuid4().hex[:12]
        session_dir = _session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        display_num = _alloc_display()
        port = _alloc_port()
        password = secrets.token_urlsafe(18)

        # Reserve the slot immediately (placeholder in _SESSIONS) so a
        # concurrent start_session() call can't pick the same display/port
        # while we spawn the (slow) subprocesses below.
        info = SessionInfo(
            session_id=session_id, key=key, target_dir=str(cwd), sch_path=str(sch_path),
            label=label, display_num=display_num, vnc_port=port, vnc_password=password,
            resolution=resolution, created_at=_now(), last_active_at=_now(),
            xvfb_pid=0, xschem_pid=0, x11vnc_pid=0,
        )
        _SESSIONS[session_id] = info

    try:
        xvfb = _spawn_xvfb(display_num, session_dir / "xvfb.log", resolution=resolution)
        if not _wait_for(lambda: (X11_SOCKET_DIR / f"X{display_num}").exists() or xvfb.poll() is not None, 5.0):
            _terminate(xvfb)
            raise LaunchFailed(f"Xvfb on display :{display_num} did not come up within 5s")
        if xvfb.poll() is not None:
            tail = (session_dir / "xvfb.log").read_text()[-800:]
            raise LaunchFailed(f"Xvfb exited immediately (code {xvfb.returncode}): {tail}")

        xschem = _spawn_xschem(sch_path, cwd, display_num, session_dir / "xschem.log", resolution=resolution)
        time.sleep(1.0)  # fast-failure window, same convention as schematics.launch_xschem
        if xschem.poll() is not None:
            _terminate(xvfb)
            tail = (session_dir / "xschem.log").read_text()[-800:]
            raise LaunchFailed(f"xschem exited immediately (code {xschem.returncode}): {tail}")

        vnc = _spawn_x11vnc(display_num, port, password, session_dir / "x11vnc.log")
        vnc_up = _wait_for(lambda: _port_open(port) or vnc.poll() is not None, 5.0)
        if vnc.poll() is not None:
            _terminate(xschem)
            _terminate(xvfb)
            tail = (session_dir / "x11vnc.log").read_text()[-800:]
            raise LaunchFailed(f"x11vnc exited immediately (code {vnc.returncode}): {tail}")
        if not vnc_up:
            _terminate(vnc)
            _terminate(xschem)
            _terminate(xvfb)
            raise LaunchFailed(f"x11vnc did not open its loopback port {port} within 5s")
    except Exception:
        with _lock:
            _SESSIONS.pop(session_id, None)
            _PROCS.pop(session_id, None)
        raise

    info.xvfb_pid = xvfb.pid
    info.xschem_pid = xschem.pid
    info.x11vnc_pid = vnc.pid
    with _lock:
        _PROCS[session_id] = {"xvfb": xvfb, "xschem": xschem, "x11vnc": vnc}
        _persist(info)
    return {**_public(info), "vnc_password": password, "reused": False}


def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def stop_session(session_id: str) -> bool:
    with _lock:
        info = _SESSIONS.pop(session_id, None)
        procs = _PROCS.pop(session_id, None)
    if info is None:
        return False
    for name in ("x11vnc", "xschem", "xvfb"):
        p = (procs or {}).get(name)
        if p is not None:
            _terminate(p)
    session_json = _session_dir(session_id) / "session.json"
    if session_json.is_file():
        session_json.unlink()
    return True


def restart_session(session_id: str, resolution: str) -> dict[str, Any]:
    """The "resize the DISPLAY" lever: stop `session_id` (reaping its
    Xvfb/xschem/x11vnc and freeing its display number + VNC port) and start a
    brand-new session at `resolution` for the exact same target (cwd/
    sch_path/label) it was showing. This is DESTRUCTIVE - Xvfb's RANDR is a
    stub in this environment (verified: `xrandr --newmode`/`--fb` cannot
    resize a running display), so genuinely changing the drawing area has no
    live-resize path; it can only be done by killing the old X server, which
    kills xschem, which means ANY UNSAVED WORK IN THE OLD SESSION IS LOST.

    This function does not itself prompt for confirmation - it trusts the
    caller (backend/main.py's restart route, in turn trusted to have gotten
    explicit confirmation from the frontend) that the user has already been
    warned. xschem exposes no reliable "has unsaved changes" signal this
    module can poll over its current control surface (no persistent Tcl
    session channel is kept open to a running xschem today), so no attempt is
    made to detect/block on that here - only the warning at the UI layer.

    Raises LaunchFailed if `session_id` doesn't exist (nothing to restart),
    ValueError for an invalid `resolution`, or whatever start_session()
    itself can raise (PrereqMissing / TooManySessions / LaunchFailed) if the
    fresh session fails to come up - in that last case the OLD session is
    already gone (stopped before the new one was attempted), matching the
    "reap the old session before starting the new one" requirement."""
    with _lock:
        info = _SESSIONS.get(session_id)
        if info is None:
            raise LaunchFailed(f"session {session_id} not found - it may have already been stopped")
        cwd = Path(info.target_dir)
        sch_path = Path(info.sch_path)
        label = info.label
    stop_session(session_id)  # reap BEFORE starting the new one, unconditionally
    return start_session(cwd, sch_path, label, resolution=resolution)


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        info = _SESSIONS.get(session_id)
        return _public(info) if info else None


def get_vnc_endpoint(session_id: str) -> Optional[tuple[str, int]]:
    """127.0.0.1 + port to proxy this session's WebSocket to, or None if the
    session doesn't exist / has died. Never returns anything but loopback -
    see the module docstring's SECURITY MODEL."""
    with _lock:
        info = _SESSIONS.get(session_id)
        procs = _PROCS.get(session_id) or {}
        if info is None or not procs or not all(p.poll() is None for p in procs.values()):
            return None
        return ("127.0.0.1", info.vnc_port)


def touch_connect(session_id: str) -> None:
    with _lock:
        info = _SESSIONS.get(session_id)
        if info is not None:
            info.clients += 1
            info.last_active_at = _now()


def touch_disconnect(session_id: str) -> None:
    with _lock:
        info = _SESSIONS.get(session_id)
        if info is not None:
            info.clients = max(0, info.clients - 1)
            info.last_active_at = _now()


def list_sessions() -> list[dict[str, Any]]:
    with _lock:
        return [_public(info) for info in _SESSIONS.values()]


# --- idle reaping --------------------------------------------------------------


def reap_idle_once(now: Optional[datetime] = None) -> list[str]:
    """Stop every session with zero connected clients whose last activity is
    older than IDLE_TIMEOUT_S. Returns the ids reaped - called both by the
    background thread below and directly by tests (no sleeping required)."""
    now = now or datetime.now(timezone.utc)
    with _lock:
        expired = [
            info.session_id for info in _SESSIONS.values()
            if info.clients == 0 and (now - _parse(info.last_active_at)).total_seconds() > IDLE_TIMEOUT_S
        ]
    for sid in expired:
        stop_session(sid)
    return expired


def start_background_reaper() -> None:
    """Start the idle-reaper thread once (idempotent - safe to call from
    main.py's startup event even if it somehow runs twice)."""
    global _reaper_thread
    if _reaper_thread is not None and _reaper_thread.is_alive():
        return

    def _loop() -> None:
        while True:
            time.sleep(REAP_INTERVAL_S)
            try:
                reap_idle_once()
            except Exception:
                pass  # never let the reaper thread die

    _reaper_thread = threading.Thread(target=_loop, daemon=True)
    _reaper_thread.start()


def cleanup_orphans_on_startup() -> int:
    """Kill any Xvfb/xschem/x11vnc left running by a killed backend process
    (found via the session.json files under xschem_sessions_root() - a fresh
    backend process has an empty in-memory registry, so EVERY persisted
    session.json at this point is, by definition, an orphan: nothing is
    proxying its VNC traffic anymore and nothing ever will again without a
    fresh start_session() call). Mirrors main.py's _repair_orphaned_channels
    pattern. Returns the number of orphaned sessions cleaned up."""
    root = xschem_sessions_root()
    count = 0
    for session_json in sorted(root.glob("*/session.json")):
        try:
            data = json.loads(session_json.read_text())
        except (json.JSONDecodeError, OSError):
            session_json.unlink(missing_ok=True)
            continue
        for pid_key in ("xvfb_pid", "xschem_pid", "x11vnc_pid"):
            pid = data.get(pid_key)
            if isinstance(pid, int) and pid > 0:
                _kill_pid(pid)
        session_json.unlink(missing_ok=True)
        count += 1
    return count
