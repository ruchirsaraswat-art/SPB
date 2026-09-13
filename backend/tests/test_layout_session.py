"""Unit checks for backend/layout.py (interactive magic-over-VNC session
manager, 2026-09 layout-editor integration) - the magic counterpart to
test_xschem_session.py, exercising the parts THIS module adds/changes:
resolve/create-blank-layout, the magic-specific spawn (rcfile + argv shape),
and the bits of xschem_session.py's generalization that only show up once a
SECOND kind exists (the concurrency cap shared across xschem + magic,
cross-kind restart guards).

Plain-python assert style, same convention as test_xschem_session.py. Run
with:
    backend/.venv/bin/python backend/tests/test_layout_session.py

No real Xvfb/magic/xschem/x11vnc are spawned - subprocess.Popen is
monkeypatched in BOTH xschem_session's and layout's own module namespaces
(xschem_session.py spawns Xvfb/x11vnc itself; layout.py spawns magic itself -
see _spawn_magic), same FakeProc idea as test_xschem_session.py.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_td = tempfile.TemporaryDirectory()
TMP = Path(_td.name)
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(TMP / "settings.json")
(TMP / "settings.json").write_text(json.dumps({"working_dir": str(TMP / "workdir")}))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xschem_session as xs  # noqa: E402
import layout  # noqa: E402

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
    """Same idea as test_xschem_session.py's FakeProc, extended to also
    recognize a "magic" argv[0] (no special socket faking needed for it -
    only Xvfb/x11vnc's readiness is genuinely polled by start_session)."""

    _next_pid = [19000]

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
    Popen = FakeProc
    STDOUT = subprocess.STDOUT


# Both modules spawn processes via their OWN `subprocess` name - see
# test_xschem_session.py's _FakeSubprocessNS docstring for why rebinding the
# module-global NAME (not the shared `subprocess` module object) matters.
xs.subprocess = _FakeSubprocessNS
layout.subprocess = _FakeSubprocessNS


def make_target(name: str) -> tuple[Path, Path]:
    cwd = TMP / name
    cwd.mkdir(parents=True, exist_ok=True)
    mag = cwd / f"{name}.mag"
    mag.write_text("magic\ntech sky130A\ntimestamp 0\n<< end >>\n")
    return cwd, mag


def _app_argv(session_id: str) -> list[str]:
    return xs._PROCS[session_id]["app"].argv


# --- prereqs / degradation ----------------------------------------------------

_orig_which = xs.shutil.which
xs.shutil.which = lambda name: None if name == "magic" else _orig_which(name) or f"/usr/bin/{name}"
status = layout.prereqs_status()
ok("layout prereqs_status flags exactly the missing binary (magic)",
   status["available"] is False and status["missing"] == ["magic"], str(status))
ok("layout prereqs_status message names the layout feature, not xschem's",
   "Interactive layout" in status["message"] and "sudo apt install magic" in status["message"], status["message"])
xs.shutil.which = _orig_which

cwd_pm, mag_pm = make_target("prereq_missing_cell")
xs.shutil.which = lambda name: None if name == "magic" else _orig_which(name) or f"/usr/bin/{name}"
try:
    layout.start_session(cwd_pm, mag_pm, "prereq missing")
    ok("PrereqMissing raised when magic is missing", False)
except xs.PrereqMissing as exc:
    ok("PrereqMissing raised when magic is missing", exc.status["missing"] == ["magic"])
xs.shutil.which = _orig_which

ok("layout prereqs_status reports available once magic is back on PATH",
   layout.prereqs_status()["available"] is True)

# --- resolve_layout / create_blank_layout / mag_and_cwd_for_cell ------------

import settings as settings_mod  # noqa: E402
libs_root = settings_mod.libraries_root()
libs_root.mkdir(parents=True, exist_ok=True)

not_found = layout.resolve_layout("nope_lib", "nope_cell")
ok("resolve_layout reports not-found for a library that doesn't exist", not_found["found"] is False)

created = layout.create_blank_layout("layout_test_lib", "test_cell", "custom", "Test Cell")
ok("create_blank_layout returns the cell dir", created["cell"] == "test_cell")
mag_path = Path(created["dir"]) / created["mag"]
ok("create_blank_layout actually wrote a .mag file", mag_path.is_file())
ok("blank .mag declares tech sky130A", "tech sky130A" in mag_path.read_text())

resolved = layout.resolve_layout("layout_test_lib", "test_cell")
ok("resolve_layout finds the freshly-created layout", resolved["found"] is True and resolved["mag"] == "test_cell.mag")

try:
    layout.create_blank_layout("layout_test_lib", "test_cell", "custom")
    ok("create_blank_layout refuses to clobber an existing .mag", False)
except layout.LayoutExists as exc:
    ok("create_blank_layout refuses to clobber an existing .mag",
       exc.library == "layout_test_lib" and exc.cell == "test_cell")

# Regression: adding a hand-drawn .mag to a cell Circuit_Builder GENERATED
# must not relabel the cell as hand-made. A generated cell carries no
# "source" key, so a setdefault() here silently stamped it "manual" and
# reset "created_at" - observed on a real filed cell (cal_runs/current_mirror)
# just by opening a layout session on it.
gen_cell = libs_root / "layout_test_lib" / "generated_cell"
gen_cell.mkdir(parents=True, exist_ok=True)
(gen_cell / "generated_cell.sch").write_text("v {xschem version=3.4.4 file_version=1.2}\n")
_gen_prov = {
    "topology": "nmos_current_mirror",
    "label": "NMOS current mirror",
    "filed_at": "2026-08-21T22:45:00+00:00",
    "views": ["generated_cell.sch"],
}
(gen_cell / "provenance.json").write_text(json.dumps(_gen_prov, indent=2))

layout.create_blank_layout("layout_test_lib", "generated_cell", "nmos_current_mirror")
_after = json.loads((gen_cell / "provenance.json").read_text())
ok("generated cell is NOT relabelled source=manual by adding a layout",
   "source" not in _after, f'source={_after.get("source")!r}')
ok("generated cell keeps its original filed_at",
   _after.get("filed_at") == "2026-08-21T22:45:00+00:00")
ok("generated cell gains no spurious created_at",
   "created_at" not in _after)
ok("the .mag IS still added to the generated cell's views",
   "generated_cell.mag" in _after.get("views", []))
ok("the generated cell's existing views are preserved",
   "generated_cell.sch" in _after.get("views", []))

# A genuinely NEW cell still gets source/created_at stamped.
_new_prov = json.loads((libs_root / "layout_test_lib" / "test_cell" / "provenance.json").read_text())
ok("a brand-new hand-made cell IS marked source=manual",
   _new_prov.get("source") == "manual")
ok("the original .mag content is untouched after the refused create",
   mag_path.read_text().count("tech sky130A") == 1)

mp, cwd_resolved = layout.mag_and_cwd_for_cell("layout_test_lib", "test_cell")
ok("mag_and_cwd_for_cell resolves the same path create_blank_layout reported", mp == mag_path)

try:
    layout.mag_and_cwd_for_cell("layout_test_lib", "does_not_exist")
    ok("mag_and_cwd_for_cell raises ValueError for a cell with no .mag", False)
except ValueError:
    ok("mag_and_cwd_for_cell raises ValueError for a cell with no .mag", True)

# A cell that already has a SCHEMATIC (no layout) must not be treated as
# having a layout - resolve_layout is layout-specific, not "any view".
sch_only_dir = libs_root / "layout_test_lib" / "sch_only_cell"
sch_only_dir.mkdir(parents=True, exist_ok=True)
(sch_only_dir / "sch_only_cell.sch").write_text("v {xschem}")
sch_only_resolved = layout.resolve_layout("layout_test_lib", "sch_only_cell")
ok("resolve_layout reports not-found for a cell that has a schematic but no layout",
   sch_only_resolved["found"] is False)

# --- start_session: kind, resolution, magic argv shape, rcfile content -----

cwd1, mag1 = make_target("live_cell_a")
r1 = layout.start_session(cwd1, mag1, "layout_test_lib/live_cell_a", resolution="1920x1200")
ok("layout.start_session returns a session_id", bool(r1.get("session_id")))
ok("layout session is tagged kind=magic", xs._SESSIONS[r1["session_id"]].kind == "magic")
ok("layout session stores the requested resolution", xs._SESSIONS[r1["session_id"]].resolution == "1920x1200")
argv1 = _app_argv(r1["session_id"])
ok("magic is spawned via -rcfile (not the default system+design rcfile lookup)",
   "-rcfile" in argv1, str(argv1))
ok("magic is spawned with -noconsole", "-noconsole" in argv1, str(argv1))
ok("magic is spawned against the exact .mag path", str(mag1) in argv1, str(argv1))
rcfile_path = Path(argv1[argv1.index("-rcfile") + 1])
ok("the generated rcfile exists on disk", rcfile_path.is_file())
rc_content = rcfile_path.read_text()
ok("the rcfile sources the PDK's sky130A.magicrc (tech load happens there)",
   "sky130A.magicrc" in rc_content, rc_content)
ok("the rcfile resizes the layout1 window to the requested resolution",
   "wm geometry .layout1 1920x1200+0+0" in rc_content, rc_content)
ok("xschemrc-style project rc was NOT written into the target cwd for magic "
   "(handled entirely via -rcfile - see layout._magicrc_content docstring)",
   not (cwd1 / "xschemrc").exists() and not (cwd1 / ".magicrc").exists())

# --- listing / cross-kind visibility -----------------------------------------

magic_only = layout.list_sessions()
ok("layout.list_sessions only returns magic-kind sessions",
   all(s["kind"] == "magic" for s in magic_only) and any(s["session_id"] == r1["session_id"] for s in magic_only))

xschem_cwd, xschem_sch = TMP / "xschem_live_cell", None
xschem_cwd.mkdir(parents=True, exist_ok=True)
xschem_sch = xschem_cwd / "xschem_live_cell.sch"
xschem_sch.write_text("v {xschem}")
xr = xs.start_session(xschem_cwd, xschem_sch, "an xschem session")
ok("a plain xschem session defaults to kind=xschem (unaffected by the generalization)",
   xs._SESSIONS[xr["session_id"]].kind == "xschem")
ok("layout.list_sessions does not leak the xschem session",
   all(s["session_id"] != xr["session_id"] for s in layout.list_sessions()))
ok("layout.get_session refuses to return an xschem-kind session by id",
   layout.get_session(xr["session_id"]) is None)
ok("xschem_session.get_session still returns it (kind-agnostic)",
   xs.get_session(xr["session_id"]) is not None)

# --- restart_session: resize + cross-kind guard ------------------------------

old_display = xs._SESSIONS[r1["session_id"]].display_num
old_procs = dict(xs._PROCS[r1["session_id"]])
restarted = layout.restart_session(r1["session_id"], "2560x1600")
ok("layout.restart_session returns a new session id", restarted["session_id"] != r1["session_id"])
ok("restarted layout session kept kind=magic", restarted["kind"] == "magic")
ok("restarted layout session runs at the new resolution", restarted["resolution"] == "2560x1600")
ok("old magic/Xvfb/x11vnc were actually terminated (reaped before the new one started)",
   all(p.poll() is not None for p in old_procs.values()))
ok("the old display number is not held by two sessions at once",
   len([s for s in xs._SESSIONS.values() if s.display_num == old_display]) <= 1)
ok("old session is gone from get_session", layout.get_session(r1["session_id"]) is None)

try:
    layout.restart_session(xr["session_id"], "1600x1000")
    ok("layout.restart_session refuses to restart an xschem-kind session id", False)
except xs.LaunchFailed:
    ok("layout.restart_session refuses to restart an xschem-kind session id", True)

layout.stop_session(restarted["session_id"])
xs.stop_session(xr["session_id"])

# --- concurrency cap enforced across BOTH tool types -------------------------

started_ids = []
started_kinds = []
i = 0
while len(started_ids) < xs.MAX_SESSIONS:
    if i % 2 == 0:
        c, s = make_target(f"cap_layout_{i}")
        sid = layout.start_session(c, s, f"cap layout {i}")["session_id"]
        started_kinds.append("magic")
    else:
        c = TMP / f"cap_xschem_{i}"
        c.mkdir(parents=True, exist_ok=True)
        sch = c / f"cap_xschem_{i}.sch"
        sch.write_text("v {xschem}")
        sid = xs.start_session(c, sch, f"cap xschem {i}")["session_id"]
        started_kinds.append("xschem")
    started_ids.append(sid)
    i += 1

ok(f"filled the shared cap ({xs.MAX_SESSIONS}) with a MIX of xschem and magic sessions",
   len(started_ids) == xs.MAX_SESSIONS and "magic" in started_kinds and "xschem" in started_kinds,
   str(started_kinds))

c_over, s_over = make_target("cap_over_layout")
try:
    layout.start_session(c_over, s_over, "cap over layout")
    ok("starting one more MAGIC session over the shared cap raises TooManySessions", False)
except xs.TooManySessions:
    ok("starting one more MAGIC session over the shared cap raises TooManySessions", True)

c_over2 = TMP / "cap_over_xschem"
c_over2.mkdir(parents=True, exist_ok=True)
s_over2 = c_over2 / "cap_over_xschem.sch"
s_over2.write_text("v {xschem}")
try:
    xs.start_session(c_over2, s_over2, "cap over xschem")
    ok("starting one more XSCHEM session over the shared cap (hit by magic sessions) also raises TooManySessions", False)
except xs.TooManySessions:
    ok("starting one more XSCHEM session over the shared cap (hit by magic sessions) also raises TooManySessions", True)

for sid in started_ids:
    xs.stop_session(sid)

# --- idle reaping applies to magic sessions too ------------------------------

cwd_idle, mag_idle = make_target("idle_layout_cell")
r_idle = layout.start_session(cwd_idle, mag_idle, "idle layout cell")
sid_idle = r_idle["session_id"]
xs._SESSIONS[sid_idle].last_active_at = (
    datetime.now(timezone.utc) - timedelta(seconds=xs.IDLE_TIMEOUT_S + 5)
).isoformat()
reaped = xs.reap_idle_once()
ok("an idle magic session is reaped by the SAME shared reaper xschem uses", sid_idle in reaped, str(reaped))
ok("reaped magic session is gone", layout.get_session(sid_idle) is None)

# --- capture_manual_layout (REAL magic binary, not mocked) ------------------
# Same gating convention as test_manual_schematic.py's real-xschem capture
# checks: skip gracefully (not a failure) if the real toolchain isn't on
# PATH, since everything above this point never touched the real magic
# binary (FakeProc stood in for it).

# Restore the REAL subprocess module in layout's namespace - everything
# above this point used FakeProc; capture_manual_layout below spawns a
# genuine magic/xvfb-run process and needs the real subprocess.run/
# TimeoutExpired.
layout.subprocess = subprocess

_have_layout_toolchain = all(shutil.which(b) for b in ("magic", "xvfb-run", "Xvfb"))
if not _have_layout_toolchain:
    print("SKIP capture_manual_layout checks - magic/xvfb-run/Xvfb not on PATH")
else:
    real_lib = "layout_capture_real_test"
    real_created = layout.create_blank_layout(real_lib, "paint_test_cell", "custom")
    real_mag_path = Path(real_created["dir"]) / real_created["mag"]
    # Paint something real onto the blank cell via a genuine (non-mocked)
    # magic batch run, so the capture below has actual non-blank content to
    # render - proves this is exercising real geometry, not just an empty
    # canvas.
    paint_script = "load paint_test_cell\nbox 0 0 200 200\npaint nwell\nsave paint_test_cell\nquit -noprompt\n"
    paint_proc = subprocess.run(
        ["xvfb-run", "-a", "magic", "-noconsole"],
        cwd=str(real_mag_path.parent), input=paint_script.encode(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
    )
    ok("real magic paints real geometry onto the blank layout (setup for the capture check)",
       paint_proc.returncode == 0 and "tech sky130A" in real_mag_path.read_text()
       and "nwell" in real_mag_path.read_text(),
       paint_proc.stdout.decode(errors="replace")[-800:])

    captured = layout.capture_manual_layout(real_lib, "paint_test_cell")
    png_path = Path(captured["dir"]) / captured["png"]
    ok("capture_manual_layout wrote a PNG", png_path.is_file())
    png_bytes = png_path.read_bytes()
    ok("the PNG has a real PNG signature", png_bytes[:8] == b"\x89PNG\r\n\x1a\n", png_bytes[:16].hex())
    ok("the PNG is non-trivially sized (real rendered content, not an empty stub)",
       len(png_bytes) > 500, str(len(png_bytes)))
    ok("capture_manual_layout uses a DIFFERENT filename than the schematic PNG convention "
       "(<cell>_layout.png, not <cell>.png - avoids clobbering a schematic capture)",
       captured["png"] == "paint_test_cell_layout.png", captured["png"])

    resolved_after_capture = layout.resolve_layout(real_lib, "paint_test_cell")
    ok("resolve_layout reports the captured PNG afterwards",
       resolved_after_capture.get("png") == captured["png"])

    try:
        layout.capture_manual_layout(real_lib, "does_not_exist")
        ok("capture_manual_layout raises ValueError for a cell with no .mag", False)
    except ValueError:
        ok("capture_manual_layout raises ValueError for a cell with no .mag", True)

print(f"\nall {CHECKS} checks passed")
