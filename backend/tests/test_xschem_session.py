"""Unit checks for backend/xschem_session.py (interactive xschem-over-VNC
session manager, 2026-09 remote-access spec).

Plain-python assert style (no pytest on this machine), same convention as
test_schematics.py / test_settings.py. Run with:
    backend/.venv/bin/python backend/tests/test_xschem_session.py

No real Xvfb/xschem/x11vnc are spawned - subprocess.Popen is monkeypatched to
a FakeProc that fakes just enough of each binary's observable behavior (Xvfb
creates its /tmp/.X11-unix/XN socket; x11vnc actually binds+listens on its
loopback port, so the module's own readiness polling is exercised for real)
to drive the whole lifecycle without needing real binaries installed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_td = tempfile.TemporaryDirectory()
TMP = Path(_td.name)
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(TMP / "settings.json")
(TMP / "settings.json").write_text(json.dumps({"working_dir": str(TMP / "workdir")}))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xschem_session as xs  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


FAKE_X11_DIR = TMP / "X11-unix"
FAKE_X11_DIR.mkdir()
xs.X11_SOCKET_DIR = FAKE_X11_DIR


class FakeProc:
    """Stands in for subprocess.Popen for Xvfb/xschem/x11vnc. Xvfb fakes
    creating (and, on terminate, removing) its X11 unix socket; x11vnc
    actually binds+listens on its loopback port so start_session()'s real
    readiness-polling code path (_port_open) is genuinely exercised."""

    _next_pid = [9000]

    def __init__(self, argv, cwd=None, env=None, stdout=None, stderr=None, start_new_session=None):
        FakeProc._next_pid[0] += 1
        self.pid = FakeProc._next_pid[0]
        self.argv = argv
        self.returncode = None
        self._sock = None
        self._xvfb_sock_path = None
        if argv[0] == "Xvfb":
            n = argv[1].lstrip(":")
            path = FAKE_X11_DIR / f"X{n}"
            path.touch()
            self._xvfb_sock_path = path
        elif argv[0] == "x11vnc":
            port = int(argv[argv.index("-rfbport") + 1])
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(1)
            self._sock = s

    def poll(self):
        return self.returncode

    def terminate(self):
        if self.returncode is not None:
            return
        self.returncode = 0
        if self._sock is not None:
            self._sock.close()
        if self._xvfb_sock_path is not None and self._xvfb_sock_path.exists():
            self._xvfb_sock_path.unlink()

    def kill(self):
        self.terminate()
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class _FakeSubprocessNS:
    """Stands in for the `subprocess` NAME inside xschem_session's own module
    namespace only - reassigning xs.subprocess.Popen directly would mutate
    the real `subprocess` module object itself (modules are singletons in
    sys.modules, so xschem_session's `import subprocess` is the SAME object
    this test file's own `import subprocess` sees), which would silently
    turn the orphan-cleanup check's real `sleep 30` process below into a
    fake one too. Rebinding the module-global NAME `subprocess` inside
    xschem_session avoids that entirely."""

    Popen = FakeProc
    STDOUT = subprocess.STDOUT


xs.subprocess = _FakeSubprocessNS


def make_target(name: str) -> tuple[Path, Path]:
    cwd = TMP / name
    cwd.mkdir(parents=True, exist_ok=True)
    sch = cwd / f"{name}.sch"
    sch.write_text("v {xschem}")
    return cwd, sch


# --- prereqs_status (real implementation, only shutil.which is faked) ------

_orig_which = xs.shutil.which
xs.shutil.which = lambda name: None if name == "x11vnc" else _orig_which(name) or f"/usr/bin/{name}"
status = xs.prereqs_status()
ok("prereqs_status flags exactly the missing binary",
   status["available"] is False and status["missing"] == ["x11vnc"], str(status))
ok("prereqs_status message names the apt package", "sudo apt install x11vnc" in status["message"], status["message"])
xs.shutil.which = _orig_which

status_ok = xs.prereqs_status()
ok("prereqs_status reports available when every binary is on PATH (this dev box has all three)",
   status_ok["available"] is True, str(status_ok))

# The rest of this suite fakes ALL THREE binaries via subprocess.Popen (never
# spawns real Xvfb/xschem/x11vnc), so force "available" unconditionally -
# real availability on this box was already proven above.
xs.prereqs_status = lambda: {"available": True, "missing": []}

# --- PrereqMissing surfaced from start_session ------------------------------

xs.prereqs_status = lambda: {"available": False, "missing": ["x11vnc"], "message": "install x11vnc"}
cwd, sch = make_target("prereq_check")
try:
    xs.start_session(cwd, sch, "prereq check")
    ok("PrereqMissing raised when a binary is missing", False)
except xs.PrereqMissing as exc:
    ok("PrereqMissing raised when a binary is missing", exc.status["missing"] == ["x11vnc"])
xs.prereqs_status = lambda: {"available": True, "missing": []}

# --- basic start/reuse/stop --------------------------------------------------

cwd1, sch1 = make_target("cell_a")
r1 = xs.start_session(cwd1, sch1, "library cell_a")
ok("start_session returns a session_id", bool(r1.get("session_id")))
ok("start_session returns a one-time vnc_password", bool(r1.get("vnc_password")))
ok("fresh session is not marked reused", r1["reused"] is False)
ok("display number allocated in the reserved range",
   xs.DISPLAY_MIN <= xs._SESSIONS[r1["session_id"]].display_num <= xs.DISPLAY_MAX)
ok("Xvfb socket file exists for the allocated display",
   (FAKE_X11_DIR / f"X{xs._SESSIONS[r1['session_id']].display_num}").exists())
ok("vnc port is actually listening (real socket connect succeeds)",
   xs._port_open(xs._SESSIONS[r1["session_id"]].vnc_port))
ok("xschemrc was written into the target cwd", (cwd1 / "xschemrc").is_file())

status_view = xs.get_session(r1["session_id"])
ok("get_session reports alive=True", status_view["alive"] is True)
ok("get_session never leaks the vnc_password", "vnc_password" not in status_view)

r1_again = xs.start_session(cwd1, sch1, "library cell_a")
ok("re-requesting the same schematic reuses the session", r1_again["reused"] is True and r1_again["session_id"] == r1["session_id"])
ok("reused response still carries the (same) password", r1_again["vnc_password"] == r1["vnc_password"])

xs.touch_connect(r1["session_id"])
ok("touch_connect increments clients", xs.get_session(r1["session_id"])["clients"] == 1)
xs.touch_disconnect(r1["session_id"])
ok("touch_disconnect decrements clients back to 0", xs.get_session(r1["session_id"])["clients"] == 0)

vnc_port = xs._SESSIONS[r1["session_id"]].display_num
stopped = xs.stop_session(r1["session_id"])
ok("stop_session reports success", stopped is True)
ok("stopped session is gone from get_session", xs.get_session(r1["session_id"]) is None)
ok("stopping an already-stopped session returns False", xs.stop_session(r1["session_id"]) is False)
ok("Xvfb socket file removed on stop (display freed)",
   not (FAKE_X11_DIR / f"X{vnc_port}").exists())

# A fresh session for a DIFFERENT target should be able to reuse that same
# now-freed display number - proves the allocator actually frees slots.
cwd2, sch2 = make_target("cell_b")
r2 = xs.start_session(cwd2, sch2, "library cell_b")
ok("freed display number is reusable by a later session",
   xs._SESSIONS[r2["session_id"]].display_num == vnc_port, str(xs._SESSIONS[r2["session_id"]].display_num))
xs.stop_session(r2["session_id"])

# --- concurrent-session cap --------------------------------------------------

started = []
for i in range(xs.MAX_SESSIONS):
    c, s = make_target(f"cap_{i}")
    started.append(xs.start_session(c, s, f"cap {i}")["session_id"])
ok(f"{xs.MAX_SESSIONS} concurrent sessions all started", len(started) == xs.MAX_SESSIONS)

c_over, s_over = make_target("cap_over")
try:
    xs.start_session(c_over, s_over, "cap over")
    ok("starting one more than the cap raises TooManySessions", False)
except xs.TooManySessions:
    ok("starting one more than the cap raises TooManySessions", True)

for sid in started:
    xs.stop_session(sid)

# --- idle reaping -------------------------------------------------------------

cwd3, sch3 = make_target("idle_cell")
r3 = xs.start_session(cwd3, sch3, "idle cell")
sid3 = r3["session_id"]
# Backdate last_active_at well past the idle timeout, with zero clients.
xs._SESSIONS[sid3].last_active_at = (
    datetime.now(timezone.utc) - timedelta(seconds=xs.IDLE_TIMEOUT_S + 5)
).isoformat()
reaped = xs.reap_idle_once()
ok("idle session (no clients) gets reaped", sid3 in reaped, str(reaped))
ok("reaped session is gone", xs.get_session(sid3) is None)

cwd4, sch4 = make_target("busy_cell")
r4 = xs.start_session(cwd4, sch4, "busy cell")
sid4 = r4["session_id"]
xs.touch_connect(sid4)
xs._SESSIONS[sid4].last_active_at = (
    datetime.now(timezone.utc) - timedelta(seconds=xs.IDLE_TIMEOUT_S + 5)
).isoformat()
reaped2 = xs.reap_idle_once()
ok("a session with a connected client is never reaped even if 'stale'", sid4 not in reaped2, str(reaped2))
xs.touch_disconnect(sid4)
xs.stop_session(sid4)

# --- orphan cleanup on startup ------------------------------------------------

orphan_dir = xs.xschem_sessions_root() / "orphan_test_session"
orphan_dir.mkdir(parents=True, exist_ok=True)
real_orphan = subprocess.Popen(["sleep", "30"])
(orphan_dir / "session.json").write_text(json.dumps({
    "session_id": "orphan_test_session", "xvfb_pid": real_orphan.pid,
    "xschem_pid": 0, "x11vnc_pid": 0,
}))
n = xs.cleanup_orphans_on_startup()
ok("cleanup_orphans_on_startup reports the orphan it found", n >= 1, str(n))
# NOTE: cleanup_orphans_on_startup() already sent SIGTERM (checked below via
# wait()); this test happens to be real_orphan's actual parent process (a
# real orphan's parent is long dead, re-parented to init, which reaps it
# promptly) - .wait() here does that same reaping so poll() reports it, not
# a zombie hanging around because nothing in this test called wait() yet.
real_orphan.wait(timeout=5)
ok("the orphaned real process was actually killed", real_orphan.poll() is not None)
ok("the orphan's session.json was removed", not (orphan_dir / "session.json").exists())

# --- display resolution (2026-09 resize feature) -----------------------------

def _xvfb_argv(session_id: str) -> list[str]:
    return xs._PROCS[session_id]["xvfb"].argv


def _xschem_argv(session_id: str) -> list[str]:
    return xs._PROCS[session_id]["xschem"].argv


cwd5, sch5 = make_target("res_cell")
r5 = xs.start_session(cwd5, sch5, "res cell", resolution="1920x1200")
sid5 = r5["session_id"]
ok("start_session with an explicit resolution stores it on the session",
   xs._SESSIONS[sid5].resolution == "1920x1200")
ok("get_session/_public reports the resolution", xs.get_session(sid5)["resolution"] == "1920x1200")
ok("Xvfb is spawned with the requested screen size",
   f"1920x1200x24" in _xvfb_argv(sid5), str(_xvfb_argv(sid5)))
ok("xschem is spawned with a --command that resizes its own Tk window to fill the display "
   "(fact: there's no window manager on these Xvfb displays to do that for it)",
   "wm geometry . 1920x1200+0+0" in _xschem_argv(sid5), str(_xschem_argv(sid5)))

r5_default = xs.start_session(*make_target("res_default_cell"), "res default cell")
ok("start_session with no resolution argument uses DEFAULT_RESOLUTION",
   xs._SESSIONS[r5_default["session_id"]].resolution == xs.DEFAULT_RESOLUTION)
xs.stop_session(r5_default["session_id"])

# --- invalid/absurd resolution rejected --------------------------------------

sessions_before = len(xs._SESSIONS)
cwd6, sch6 = make_target("bad_res_cell")
try:
    xs.start_session(cwd6, sch6, "bad res cell", resolution="99999x99999")
    ok("an absurd resolution not in ALLOWED_RESOLUTIONS raises ValueError", False)
except ValueError as exc:
    ok("an absurd resolution not in ALLOWED_RESOLUTIONS raises ValueError", "99999x99999" in str(exc), str(exc))
ok("rejecting a bad resolution never allocates/leaks a session",
   len(xs._SESSIONS) == sessions_before, f"{len(xs._SESSIONS)} != {sessions_before}")
try:
    xs.start_session(cwd6, sch6, "bad res cell", resolution="not-a-resolution")
    ok("a non-WxH-shaped resolution string also raises ValueError", False)
except ValueError:
    ok("a non-WxH-shaped resolution string also raises ValueError", True)

# --- restart_session: resize the DISPLAY (destructive) -----------------------

old_display = xs._SESSIONS[sid5].display_num
old_port = xs._SESSIONS[sid5].vnc_port
old_procs = dict(xs._PROCS[sid5])  # keep references - stop_session() will pop them from _PROCS
old_socket = FAKE_X11_DIR / f"X{old_display}"
ok("old session's Xvfb socket exists before restart", old_socket.exists())

restarted = xs.restart_session(sid5, "2560x1600")
ok("restart_session returns a NEW session_id (old one was reaped, not mutated in place)",
   restarted["session_id"] != sid5, str(restarted))
ok("restarted session reports 'reused': False (it's a genuinely fresh session)",
   restarted["reused"] is False)
ok("restarted session runs at the newly requested resolution",
   restarted["resolution"] == "2560x1600", str(restarted))
ok("restarted session targets the SAME schematic file the old one did",
   restarted["sch_path"] == str(sch5))
ok("old session is gone from the registry", xs.get_session(sid5) is None)
ok("old Xvfb/xschem/x11vnc were actually terminated (FakeProc.poll() no longer None)",
   all(p.poll() is not None for p in old_procs.values()))
# NOTE: the OLD display number is legitimately REUSABLE the instant it's
# freed (proven separately above by "freed display number is reusable by a
# later session") - restart_session()'s fresh start_session() call may well
# land on that exact same number again via _alloc_display()'s lowest-free
# search, which would re-touch the same socket PATH for the NEW session.
# That's correct behavior, not a leak - what actually matters is that the
# display slot is never held by two sessions at once (no leaked duplicate)
# and the OLD process (checked above) was genuinely torn down first.
live_owners_of_old_display = [
    sid for sid, info in xs._SESSIONS.items() if info.display_num == old_display
]
ok("the old display number is owned by at most the ONE new session, never the old one too "
   "(no leaked duplicate claim on the same display)",
   live_owners_of_old_display in ([], [restarted["session_id"]]), str(live_owners_of_old_display))
ok("new session's Xvfb argv carries the new resolution",
   "2560x1600x24" in _xvfb_argv(restarted["session_id"]))

new_sid5 = restarted["session_id"]

# Restarting a session that no longer exists (e.g. already reaped) fails
# clearly rather than silently doing nothing.
try:
    xs.restart_session("not-a-real-session-id", "1280x800")
    ok("restarting a nonexistent session raises LaunchFailed", False)
except xs.LaunchFailed:
    ok("restarting a nonexistent session raises LaunchFailed", True)

xs.stop_session(new_sid5)

# --- concurrency cap still enforced across restarts --------------------------

cap_started = []
for i in range(xs.MAX_SESSIONS):
    c, s = make_target(f"cap_restart_{i}")
    cap_started.append(xs.start_session(c, s, f"cap restart {i}")["session_id"])
ok(f"filled the cap with {xs.MAX_SESSIONS} sessions", len(xs._SESSIONS) == xs.MAX_SESSIONS)

# Restarting ONE of them (reap-then-start-again) must succeed even while
# sitting exactly at the cap - it frees its own slot before reclaiming it.
restarted_at_cap = xs.restart_session(cap_started[0], "1600x1000")
ok("restarting a session while at the concurrency cap succeeds (reaps its own slot first)",
   bool(restarted_at_cap.get("session_id")))
ok("session count is still exactly at the cap after a restart (net zero change)",
   len(xs._SESSIONS) == xs.MAX_SESSIONS, str(len(xs._SESSIONS)))
cap_started[0] = restarted_at_cap["session_id"]

c_over2, s_over2 = make_target("cap_restart_over")
try:
    xs.start_session(c_over2, s_over2, "cap restart over")
    ok("the cap is still enforced for a genuinely NEW session after a restart", False)
except xs.TooManySessions:
    ok("the cap is still enforced for a genuinely NEW session after a restart", True)

for sid in cap_started:
    xs.stop_session(sid)

print(f"\nall {CHECKS} checks passed")
