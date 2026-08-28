"""Unit checks for backend/settings.py + the request-time path resolution it
feeds (cycle 5 configurable working dir).

Plain-python assert style (no pytest on this machine). Run with:
    backend/.venv/bin/python backend/tests/test_settings.py
Exits non-zero on the first failure; prints a per-check pass line otherwise.

The settings file is pointed at a temp path via ANALOG_SPEC_TOOL_SETTINGS
before any module import, so these tests never touch the real settings.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_td = tempfile.TemporaryDirectory()
TMP = Path(_td.name)
os.environ["ANALOG_SPEC_TOOL_SETTINGS"] = str(TMP / "settings.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as st  # noqa: E402
import libraries as lb  # noqa: E402
import schematics as sm  # noqa: E402
import circuit_builder as cb  # noqa: E402
import main  # noqa: E402

CHECKS = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        print(f"FAIL [{CHECKS}] {name} {detail}")
        sys.exit(1)
    print(f"ok   [{CHECKS}] {name}")


def raises_value_error(fn, *args) -> str | None:
    try:
        fn(*args)
    except ValueError as exc:
        return str(exc)
    return None


# --- defaults ----------------------------------------------------------------

payload = st.settings_payload()
ok("default working dir = checkout root", payload["working_dir"] == str(st.DEFAULT_WORKING_DIR))
ok("default flagged as default", payload["is_default"] is True)
ok(
    "derived sub-paths under working dir",
    payload["derived"]["libraries"].endswith("/libraries")
    and payload["derived"]["runs"].endswith("/runs")
    and payload["derived"]["research_runs"].endswith("/research_runs"),
)

# --- validation --------------------------------------------------------------

ok("empty path rejected", raises_value_error(st.validate_working_dir, "") is not None)
ok("blank path rejected", raises_value_error(st.validate_working_dir, "   ") is not None)
ok(
    "relative path rejected",
    "absolute" in (raises_value_error(st.validate_working_dir, "some/relative/dir") or ""),
)

msg = raises_value_error(st.validate_working_dir, str(st.BASE_DIR / "backend" / "data"))
ok("path inside backend/ rejected", msg is not None and "source tree" in msg)
msg = raises_value_error(st.validate_working_dir, str(st.BASE_DIR / "frontend"))
ok("frontend/ itself rejected", msg is not None and "source tree" in msg)
ok(
    "checkout root itself allowed (it is the default)",
    st.validate_working_dir(str(st.BASE_DIR)) == st.BASE_DIR,
)

# ~ expansion: point HOME at the temp dir so no real-home dirs get created.
_real_home = os.environ.get("HOME")
os.environ["HOME"] = str(TMP)
try:
    expanded = st.validate_working_dir("~/tilde_wd")
    ok("~ expanded to an absolute path", expanded == TMP / "tilde_wd" and expanded.is_dir())
finally:
    if _real_home is not None:
        os.environ["HOME"] = _real_home

nested = TMP / "a" / "b" / "c"
ok("create-if-missing (nested)", st.validate_working_dir(str(nested)) == nested and nested.is_dir())

ro = TMP / "readonly"
ro.mkdir()
ro.chmod(0o555)
try:
    msg = raises_value_error(st.validate_working_dir, str(ro))
    ok("unwritable dir rejected with clear error", msg is not None and "not writable" in msg)
    msg = raises_value_error(st.validate_working_dir, str(ro / "sub"))
    ok("uncreatable dir rejected with clear error", msg is not None and "could not create" in msg)
finally:
    ro.chmod(0o755)

file_in_the_way = TMP / "iam_a_file"
file_in_the_way.write_text("x")
ok(
    "path that is a file rejected",
    raises_value_error(st.validate_working_dir, str(file_in_the_way)) is not None,
)

# --- set/persist + previous-roots bookkeeping --------------------------------

wd_a = TMP / "wd_a"
wd_b = TMP / "wd_b"

payload = st.set_working_dir(str(wd_a))
ok("set working dir A", payload["working_dir"] == str(wd_a) and payload["is_default"] is False)
ok(
    "old (default) dir recorded as previous",
    [p["working_dir"] for p in payload["previous_working_dirs"]] == [str(st.DEFAULT_WORKING_DIR)],
)
on_disk = json.loads((TMP / "settings.json").read_text())
ok("settings persisted to disk", on_disk["working_dir"] == str(wd_a))

payload = st.set_working_dir(str(wd_b))
ok(
    "previous list is newest-config first",
    [p["working_dir"] for p in payload["previous_working_dirs"]]
    == [str(wd_a), str(st.DEFAULT_WORKING_DIR)],
)

payload = st.set_working_dir(str(wd_b))
ok(
    "re-setting same dir does not duplicate previous entries",
    [p["working_dir"] for p in payload["previous_working_dirs"]]
    == [str(wd_a), str(st.DEFAULT_WORKING_DIR)],
)

payload = st.set_working_dir(str(wd_a))
ok(
    "switching back removes the now-current dir from previous",
    payload["working_dir"] == str(wd_a)
    and str(wd_a) not in [p["working_dir"] for p in payload["previous_working_dirs"]]
    and [p["working_dir"] for p in payload["previous_working_dirs"]][0] == str(wd_b),
)

# --- request-time resolution (no restart, no module reload) ------------------

st.set_working_dir(str(wd_a))
ok("runs_root derives from current working dir", st.runs_root() == wd_a / "runs" and st.runs_root().is_dir())
res = lb.create_library("lib_in_a")
ok("library created under current (A) root", res["path"] == str(wd_a / "libraries" / "lib_in_a"))

st.set_working_dir(str(wd_b))  # nothing reloaded - same interpreter, same modules
ok("runs_root follows the settings change immediately", st.runs_root() == wd_b / "runs")
res = lb.create_library("lib_in_b")
ok("library created under new (B) root", res["path"] == str(wd_b / "libraries" / "lib_in_b"))

# --- previous-roots search ---------------------------------------------------

names = {l["name"]: l for l in lb.list_libraries()}
ok("listing includes current-root library", "lib_in_b" in names)
ok(
    "listing still includes previous-root library (data not moved)",
    "lib_in_a" in names and names["lib_in_a"]["root"] == str(wd_a / "libraries"),
)
res = lb.create_library("lib_in_a")
ok(
    "creating an existing previous-root library reports it, does not shadow",
    res["already_existed"] is True and res["path"] == str(wd_a / "libraries" / "lib_in_a"),
)
ok(
    "find_library_dir searches previous roots",
    lb.find_library_dir("lib_in_a") == wd_a / "libraries" / "lib_in_a",
)

# Filed cell in the OLD root must still resolve for the schematic sub-window.
cell_dir = wd_a / "libraries" / "cal_lib" / "mirror_cell"
cell_dir.mkdir(parents=True)
(cell_dir / "mirror_cell.png").write_bytes(b"\x89PNG fake")
(cell_dir / "mirror_cell.sch").write_text("v {xschem}")
(cell_dir / "provenance.json").write_text(json.dumps({
    "run_id": "cafecafe0000",
    "topology": "nmos_current_mirror",
    # Far-future filed_at: the real default checkout root is (correctly) also
    # on the previous-roots search path here, and its genuinely filed
    # current-mirror cells must not outrank this test fixture.
    "filed_at": "2099-01-01T00:00:00+00:00",
}))
r = sm.resolve_schematic("nmos_current_mirror", [])
ok(
    "schematic resolution finds filed cell at previous root",
    r["found"] is True and r["source"] == "library" and r["dir"] == str(cell_dir),
)
ok(
    "resolved png_url uses the root-agnostic library endpoint",
    r["png_url"] == "/api/libraries/cal_lib/mirror_cell/file/mirror_cell.png",
)

# A run left at the old root stays findable too (file serving / xschem open).
old_run = wd_a / "runs" / "runoldroot1"
old_run.mkdir(parents=True)
(old_run / "status.json").write_text(json.dumps({"run_id": "runoldroot1", "state": "done", "status": "success"}))
ok("main._find_run_dir searches previous roots", main._find_run_dir("runoldroot1") == old_run)
ok("main._find_run_dir prefers/handles missing ids", main._find_run_dir("nosuchrunid") is None)

# Registry rescan (what PUT /api/settings triggers) picks up old-root runs.
main._load_runs_from_disk()
ok("run registry rescan picks up previous-root run", "runoldroot1" in main._runs)

# --- libraries_dir override (cycle 6 split) ----------------------------------

st.set_working_dir(str(wd_a))  # back to a known-clean current working dir
ok(
    "libraries_dir unset -> derived path under working dir",
    st.libraries_dir_override() is None and st.libraries_root() == wd_a / "libraries",
)
payload = st.settings_payload()
ok(
    "settings_payload reflects unset override",
    payload["libraries_dir"] is None
    and payload["libraries_dir_is_default"] is True
    and payload["libraries_root"] == str(wd_a / "libraries")
    and payload["default_libraries_dir"] == str(wd_a / "libraries"),
)

libs_x = TMP / "libs_x"
msg = raises_value_error(st.validate_libraries_dir, str(st.BASE_DIR / "backend" / "libdata"))
ok("libraries_dir inside backend/ rejected", msg is not None and "source tree" in msg)

payload = st.set_libraries_dir(str(libs_x))
ok(
    "libraries_dir override applied",
    payload["libraries_dir"] == str(libs_x)
    and payload["libraries_dir_is_default"] is False
    and payload["libraries_root"] == str(libs_x),
)
ok("libraries_root() honors the override", st.libraries_root() == libs_x)

res = lb.create_library("lib_in_override")
ok(
    "new library files under the override, not the working dir",
    res["path"] == str(libs_x / "lib_in_override"),
)
names = {l["name"]: l for l in lb.list_libraries()}
ok(
    "listing still includes libraries filed at the working-dir location before the override",
    "lib_in_a" in names,
)

libs_y = TMP / "libs_y"
payload = st.set_libraries_dir(str(libs_y))
ok(
    "switching the override records the old one as previous",
    [p["libraries_dir"] for p in payload["previous_libraries_dirs"]] == [str(libs_x)],
)
names = {l["name"]: l for l in lb.list_libraries()}
ok(
    "listing still finds a library filed under the previous override location",
    "lib_in_override" in names and names["lib_in_override"]["root"] == str(libs_x),
)
ok(
    "find_library_dir searches previous libraries_dir overrides",
    lb.find_library_dir("lib_in_override") == libs_x / "lib_in_override",
)

payload = st.set_libraries_dir("")
ok(
    "clearing the override reverts to the working-dir-derived path",
    payload["libraries_dir"] is None
    and payload["libraries_dir_is_default"] is True
    and payload["libraries_root"] == str(wd_a / "libraries"),
)
ok(
    "clearing keeps the just-cleared override in previous list",
    [p["libraries_dir"] for p in st.settings_payload()["previous_libraries_dirs"]]
    == [str(libs_y), str(libs_x)],
)
names = {l["name"]: l for l in lb.list_libraries()}
ok(
    "after clearing, both override locations still resolve for listing",
    "lib_in_override" in names,
)

# --- xschemrc + write-deny list track every root -----------------------------

fake_run = wd_b / "runs" / "fake"
fake_run.mkdir(parents=True)
cb._write_project_xschemrc(fake_run)
rc = (fake_run / "xschemrc").read_text()
ok(
    "xschemrc puts current AND previous libraries roots on XSCHEM_LIBRARY_PATH",
    str(wd_b / "libraries") in rc and str(wd_a / "libraries") in rc,
)
denies = cb.disallowed_tools()
ok(
    "Circuit_Builder write-deny list covers current and previous libraries roots",
    f"Write({wd_b / 'libraries'}/**)" in denies and f"Write({wd_a / 'libraries'}/**)" in denies,
)

print(f"\nall {CHECKS} checks passed")
_td.cleanup()
