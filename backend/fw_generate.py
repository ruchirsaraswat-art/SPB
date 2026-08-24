"""
Firmware register-layer codegen (POST /api/fw_generate) - the one v1
generation action of the firmware workspace (feature spec:
audits/2026-08-23-firmware-section-spec.md, section d).

One headless Firmware_Coder invocation turns the stored interface definition
into running, tested C in <working_dir>/firmware/<phy_type>/ (the
Firmware_Coder charter location): a generated register map header, a driver
with power-up/down sequences derived from the interface sequences, a host
stub register model, host unit tests, and a Makefile.

THE HONESTY CHECK IS SERVER-ENFORCED: after the agent reports done, this
module re-runs the verification ITSELF - a direct
`gcc -std=c99 -Wall -Werror` compile of the deliverables AND
`make -C <dir> test` - and reports status "pass" ONLY if both exit 0. The
agent's own claim is never trusted (same honesty philosophy as
circuit_builder.evaluate_summary's pass-token checks). On failure the
artifacts are kept and the compile/test log tail comes back in the response.

Deterministic signal->register map rule (part of the spec, so the generator
and the tests agree; packing multiple fields per register is explicitly v2):
  - include interface signals with group in {control, status, reset, power}
    and direction in {d2a, a2d}; exclude direction "ext" (and with the group
    filter that also excludes clock and word-rate data buses - not
    CSR-reachable);
  - order: control, status, reset, power; table order within a group;
  - one 32-bit register per signal; offset = 4 * index; field at bits
    [width-1:0];
  - d2a -> RW (readable back), a2d -> RO (stub ignores writes);
  - names: <IF>_<SIGNAL> uppercased, with <if> = the interface name with "-"
    mapped to "_" (e.g. SERDES_RX_IF_CTLE_PEAK_OFFSET).

Run bookkeeping mirrors the other paid runs (Token_Optimizer visibility):
each invocation gets <working_dir>/fw_runs/<run_id>/ with spec.json,
prompt.txt, session.log, claude_stream.jsonl and status.json (incl.
cost_usd/duration).
"""

from __future__ import annotations

import json
import re
import select
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from circuit_builder import PROMPT_PREFLIGHT_LINE, _format_event, disallowed_tools
from interfaces import normalize_interface, validate_interface
from invocation import booked_cost_usd, model_args, preflight_write_check
from settings import firmware_root, fw_runs_root, working_dir

CLAUDE_BIN = "claude"
FW_GENERATE_TIMEOUT_S = 900  # one codegen agent run - minutes, not tens of minutes
POLL_INTERVAL_S = 1.0

# Default MMIO base of the PHY CSR window (deterministic so regenerated code
# and its tests agree; a real SoC map can override via the reg_access_driver
# catalog block's base_addr field in a future spec).
DEFAULT_BASE_ADDR = 0x4001_0000

VALID_PHY_TYPES = ("ser-des", "ddr", "lpddr", "hbm", "optical", "die-to-die")

# Register-map group order (the rule above).
_GROUP_ORDER = ("control", "status", "reset", "power")

# Narrow allow-list for the codegen session: file authoring in cwd (the
# firmware output dir - relative "(**)" glob, same hard-learned scoping rule
# as circuit_builder.py), gcc/make/the test binary, and read-only helpers.
ALLOWED_TOOLS = [
    "Write(**)",
    "Edit(**)",
    "Read",
    "Glob",
    "Grep",
    "Bash(gcc *)",
    "Bash(make *)",
    "Bash(./test_*)",
    "Bash(ls *)",
    "Bash(ls)",
    "Bash(cat *)",
    "Bash(pwd)",
]


def if_c_name(interface_name: str) -> str:
    """The <if> C identifier stem: interface name with '-' mapped to '_'
    (spec rule), any other non-identifier character mapped to '_' as safety,
    and a leading underscore prefix if it would start with a digit."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", str(interface_name or "phy_if").replace("-", "_"))
    return stem if re.match(r"[A-Za-z_]", stem) else f"_{stem}"


def compute_register_map(iface: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the deterministic map rule to a normalized interface. Returns an
    ordered list of {signal, offset, width, access, group, meaning}."""
    regs: list[dict[str, Any]] = []
    for group in _GROUP_ORDER:
        for sig in iface.get("signals", []):
            if sig.get("group") != group:
                continue
            if sig.get("direction") not in ("d2a", "a2d"):
                continue
            regs.append(
                {
                    "signal": sig["name"],
                    "offset": 4 * len(regs),
                    "width": sig.get("width") or 1,
                    "access": "RW" if sig.get("direction") == "d2a" else "RO",
                    "group": group,
                    "meaning": sig.get("meaning") or "",
                }
            )
    return regs


def deliverable_files(if_name: str) -> list[str]:
    """The fixed deliverable set for one interface (spec table). Shared by
    the prompt, the honesty check and the tests so the contract can't drift."""
    return [
        f"{if_name}_regs.h",
        f"{if_name}_drv.h",
        f"{if_name}_drv.c",
        "reg_stub.h",
        "reg_stub.c",
        f"test_{if_name}.c",
        "Makefile",
    ]


def _register_table_block(if_name: str, regs: list[dict[str, Any]]) -> str:
    prefix = if_name.upper()
    lines = [
        f"| {prefix}_{r['signal'].upper()} | 0x{r['offset']:04X} | [{r['width'] - 1}:0] "
        f"| {r['access']} | {r['group']} |"
        for r in regs
    ]
    return (
        "| register | offset | field bits | access | group |\n"
        "|---|---|---|---|---|\n" + "\n".join(lines)
    )


def build_fw_generate_prompt(
    phy_type: str, iface: dict[str, Any], if_name: str, regs: list[dict[str, Any]]
) -> str:
    files = deliverable_files(if_name)
    prefix = if_name.upper()
    return PROMPT_PREFLIGHT_LINE + f"""Generate and verify the REGISTER LAYER firmware for the {phy_type} PHY's
AFE<->controller interface, in the CURRENT WORKING DIRECTORY ONLY (it is
<working_dir>/firmware/{phy_type}/ per your charter). Do not touch any files
outside this directory.

SCOPE - REGISTER-LAYER CODEGEN ONLY (v1): register map header, driver with
power-up/down sequences, host stub register model, host unit tests, and a
Makefile. NO supervisor/algorithm code (training, cal policy), NO host API
implementation, NO interrupt wiring, NO field packing, NO RTOS, NO target
toolchain (host gcc only - flag in comments where a real target toolchain
would matter). C99, no dynamic allocation, all hardware access through the
`fw_reg_read32`/`fw_reg_write32` port layer.

## Interface definition (SOURCE OF TRUTH - never invent register fields)
```json
{json.dumps(iface, indent=2)}
```

## Register map (DETERMINISTIC - generate EXACTLY this, the tool's honesty
## check and future regenerations depend on it)
Rule applied: signals with group in {{control, status, reset, power}} and
direction in {{d2a, a2d}}, ordered control -> status -> reset -> power (table
order within a group); one 32-bit register per signal, offset = 4 * index,
field at bits [width-1:0]; d2a = RW, a2d = RO (stub ignores writes).
Base address: #define {prefix}_BASE_ADDR 0x{DEFAULT_BASE_ADDR:08X}u

{_register_table_block(if_name, regs)}

That is {len(regs)} registers total. Signals excluded by the rule (ext
direction, clock group, word-rate data buses) get NO register - they are not
CSR-reachable.

## Deliverables (exact filenames)
- `{files[0]}` - the register map: `#define {prefix}_BASE_ADDR`, per-register
  `{prefix}_<SIGNAL>_OFFSET`, per-field `_MASK`/`_SHIFT`/`_WIDTH`, RO/RW
  access tag in a comment, traceable 1:1 to the interface signal names above.
- `{files[1]}` / `{files[2]}` - the driver:
  `extern uint32_t fw_reg_read32(uint32_t addr);` and
  `extern void fw_reg_write32(uint32_t addr, uint32_t v);` as the port layer
  (declared, NOT defined, in the driver - the stub defines them); per-register
  get/set accessors; `int {if_name}_power_up(const {if_name}_timing_t *t)` and
  `int {if_name}_power_down(void)` generated from the interface
  power_sequence/reset_sequence: poll the wait_for status fields with timeout
  parameters - NO hard-coded waits: every time value lives in a
  `{if_name}_timing_t` struct whose default initializer is seeded from the
  sequence values above (cite the sequence step in a comment per field).
  Retry annotations in the sequence (e.g. "retry from 4, 3x then ERROR")
  become bounded retry loops. Timeouts/failures return negative error codes.
- `{files[3]}` / `{files[4]}` - host stub register model: array-backed
  register file covering the map above, RO-write-ignored, status-bit
  injection hooks (so a test can assert pll_locked etc.), and an
  access-order log for sequence tests.
- `{files[5]}` - host unit tests (plain C, no framework), covering at least:
  offset uniqueness + 4-byte alignment across the map; field mask/shift
  round-trip per signal; RO write ignored (write an RO register via the
  driver, read back unchanged); `{if_name}_power_up` CSR access order matches
  the power_sequence step order (via the stub's access log) when status bits
  are injected; and the timeout path returns the error code when a required
  status bit is never injected. main() returns 0 only if every check passed,
  printing one line per check.
- `{files[6]}` - with a `test` target:
  `gcc -std=c99 -Wall -Werror -o test_{if_name} {files[2]} {files[4]} {files[5]}`
  then `./test_{if_name}`.

## Verify it YOURSELF before reporting done
Run `make test` in this directory and fix anything until gcc (with
-std=c99 -Wall -Werror) exits 0 AND the test binary exits 0. The tool
re-runs `gcc -std=c99 -Wall -Werror` and `make test` itself afterwards and
only reports pass on exit 0 - an unverified claim of success will be caught.

Finish with a brief (2-4 sentence) plain-English summary of what you
generated and the test outcome as your final reply.
"""


@dataclass
class FwGenerateResult:
    status: str  # "pass" | "fail"
    run_id: str
    reason: str = ""
    files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    compile_ok: bool = False
    tests_ok: bool = False
    test_output: str = ""
    cost_usd: float | None = None
    duration_s: int | None = None


def run_firmware_coder(
    prompt: str, fw_dir: Path, run_dir: Path, timeout_s: int = FW_GENERATE_TIMEOUT_S
) -> dict[str, Any]:
    """One headless Firmware_Coder codegen session, cwd = the firmware output
    dir (so the relative Write(**) scope is exactly that tree). Streams
    session.log / claude_stream.jsonl into run_dir. Returns {ok, reason,
    cost_usd}. Module-level and monkeypatchable so the endpoint tests can
    stub the agent and still exercise the real honesty check."""
    # Fail-fast preflight (Token_Optimizer audit item 2): both dirs the
    # session touches must be writable before any money is spent.
    preflight_error = preflight_write_check(fw_dir) or preflight_write_check(run_dir)
    if preflight_error:
        return {"ok": False, "reason": preflight_error, "cost_usd": 0.0}

    # Model routing (audit item 1): fw_generate is Sonnet-routed. The
    # Firmware_Coder charter also says `model: sonnet`, but the explicit
    # --model flag is the mechanism this codebase relies on (it wins over
    # frontmatter per the CLI docs) - see invocation.MODEL_ROUTING.
    cmd = [CLAUDE_BIN, "--agent", "Firmware_Coder", *model_args("fw_generate")]
    for tool in ALLOWED_TOOLS:
        cmd += ["--allowedTools", tool]
    for tool in disallowed_tools():
        cmd += ["--disallowedTools", tool]
    cmd += ["--output-format", "stream-json", "--verbose", "-p", prompt]

    started = time.time()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(fw_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return {"ok": False, "reason": f"`{CLAUDE_BIN}` CLI not found on PATH", "cost_usd": None}

    final_event: dict[str, Any] | None = None
    timed_out = False
    with open(run_dir / "session.log", "w") as human_f, open(
        run_dir / "claude_stream.jsonl", "w"
    ) as raw_f:
        human_f.write(f"[session] launching Firmware_Coder codegen in {fw_dir}\n")
        human_f.flush()
        while True:
            if time.time() - started > timeout_s:
                timed_out = True
                proc.terminate()
                break
            ready, _, _ = select.select([proc.stdout], [], [], POLL_INTERVAL_S)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if line == "":
                break
            raw_f.write(line)
            raw_f.flush()
            try:
                ev = json.loads(line)
                if ev.get("type") == "result":
                    final_event = ev
            except json.JSONDecodeError:
                pass
            formatted = _format_event(line)
            if formatted:
                human_f.write(formatted + "\n")
                human_f.flush()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        # Drain output flushed on the way down - it may include the final
        # result event with the real cost (audit item 4).
        try:
            for line in proc.stdout:
                raw_f.write(line)
                try:
                    ev = json.loads(line)
                    if ev.get("type") == "result":
                        final_event = ev
                except json.JSONDecodeError:
                    pass
        except (OSError, ValueError):
            pass

    # Cost booking on abnormal endings (audit item 4).
    failed_cost = booked_cost_usd(run_dir / "claude_stream.jsonl")
    if timed_out:
        return {"ok": False, "reason": f"Firmware_Coder timed out after {timeout_s}s",
                "cost_usd": failed_cost}
    if proc.returncode != 0:
        return {"ok": False, "reason": f"claude CLI exited with code {proc.returncode}",
                "cost_usd": failed_cost}
    if final_event is None:
        return {"ok": False, "reason": "claude CLI exited without a final result event",
                "cost_usd": failed_cost}
    cost = final_event.get("total_cost_usd")
    if final_event.get("is_error"):
        return {"ok": False, "reason": "Firmware_Coder session reported an error", "cost_usd": cost}
    return {"ok": True, "reason": "", "cost_usd": cost}


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:] if len(text) > n else text


def run_honesty_check(fw_dir: Path, if_name: str) -> tuple[bool, bool, str]:
    """The server-side verification the response's pass/fail is based on:
      1. a DIRECT `gcc -std=c99 -Wall -Werror` compile of the deliverables
         (enforces the flags even if the Makefile drops them) -> compile_ok;
      2. `make -C <fw_dir> test` (the shipped Makefile compiling AND running
         the test binary) -> tests_ok (exit 0 only).
    Returns (compile_ok, tests_ok, combined_output)."""
    out_parts: list[str] = []
    gcc_cmd = [
        "gcc", "-std=c99", "-Wall", "-Werror",
        "-o", f"test_{if_name}",
        f"{if_name}_drv.c", "reg_stub.c", f"test_{if_name}.c",
    ]
    try:
        gcc = subprocess.run(
            gcc_cmd, cwd=str(fw_dir), capture_output=True, text=True, timeout=120
        )
        compile_ok = gcc.returncode == 0
        out_parts.append(f"$ {' '.join(gcc_cmd)}\n{gcc.stdout}{gcc.stderr}(exit {gcc.returncode})")
    except (OSError, subprocess.TimeoutExpired) as exc:
        compile_ok = False
        out_parts.append(f"$ {' '.join(gcc_cmd)}\n{exc}")

    tests_ok = False
    if compile_ok:
        make_cmd = ["make", "-C", str(fw_dir), "test"]
        try:
            make = subprocess.run(make_cmd, capture_output=True, text=True, timeout=120)
            tests_ok = make.returncode == 0
            out_parts.append(f"$ make test\n{make.stdout}{make.stderr}(exit {make.returncode})")
        except (OSError, subprocess.TimeoutExpired) as exc:
            out_parts.append(f"$ make test\n{exc}")
    else:
        out_parts.append("(make test skipped: direct gcc compile failed)")
    return compile_ok, tests_ok, _tail("\n\n".join(out_parts))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fw_generate(phy_type: str, interface: Any) -> FwGenerateResult:
    """The whole codegen action: validate, prompt, invoke Firmware_Coder,
    then server-enforce the compile+test honesty check. Raises ValueError
    (-> 422) for an invalid phy_type or an interface with hard validation
    errors; every downstream failure comes back as status='fail' with the
    artifacts kept and log tails in test_output."""
    if phy_type not in VALID_PHY_TYPES:
        raise ValueError(
            f"unknown phy_type {phy_type!r} - must be one of: {', '.join(VALID_PHY_TYPES)}"
        )
    iface = normalize_interface(interface)
    errors, _warnings = validate_interface(iface)
    if errors:
        raise ValueError(
            "interface definition failed validation:\n" + "\n".join(f"- {e}" for e in errors)
        )
    regs = compute_register_map(iface)
    if not regs:
        raise ValueError(
            "the interface has no CSR-reachable signals (group control/status/reset/power "
            "with direction d2a/a2d) - nothing to generate a register layer from"
        )

    if_name = if_c_name(iface.get("name"))
    files = deliverable_files(if_name)
    run_id = uuid.uuid4().hex[:12]
    run_dir = fw_runs_root() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    fw_dir = firmware_root() / phy_type
    fw_dir.mkdir(parents=True, exist_ok=True)

    prompt = build_fw_generate_prompt(phy_type, iface, if_name, regs)
    (run_dir / "prompt.txt").write_text(prompt)
    (run_dir / "spec.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "fw_generate",
                "phy_type": phy_type,
                "if_name": if_name,
                "interface_name": iface.get("name"),
                "register_map": regs,
                "expected_files": files,
                "output_dir": str(fw_dir),
                "created_at": _now(),
            },
            indent=2,
        )
    )

    started = time.time()
    agent = run_firmware_coder(prompt, fw_dir, run_dir)

    missing = [f for f in files if not (fw_dir / f).is_file()]
    compile_ok = False
    tests_ok = False
    test_output = ""
    if agent["ok"] and not missing:
        compile_ok, tests_ok, test_output = run_honesty_check(fw_dir, if_name)
        reason = "" if (compile_ok and tests_ok) else (
            "server-side verification failed: "
            + ("unit tests failed" if compile_ok else "gcc -std=c99 -Wall -Werror compile failed")
        )
    elif not agent["ok"]:
        reason = agent["reason"]
    else:
        reason = "Firmware_Coder finished but deliverables are missing on disk: " + ", ".join(missing)

    status = "pass" if (agent["ok"] and not missing and compile_ok and tests_ok) else "fail"
    rel_root = working_dir()
    present = [
        str((fw_dir / f).relative_to(rel_root)) if fw_dir.is_relative_to(rel_root) else str(fw_dir / f)
        for f in files
        if (fw_dir / f).is_file()
    ]
    result = FwGenerateResult(
        status=status,
        run_id=run_id,
        reason=reason,
        files=present,
        missing_files=missing,
        compile_ok=compile_ok,
        tests_ok=tests_ok,
        test_output=test_output,
        cost_usd=agent.get("cost_usd"),
        duration_s=int(time.time() - started),
    )
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "fw_generate",
                "phy_type": phy_type,
                "state": "done",
                "status": status,
                "reason": reason,
                "files": result.files,
                "missing_files": missing,
                "compile_ok": compile_ok,
                "tests_ok": tests_ok,
                "cost_usd": result.cost_usd,
                "duration_s": result.duration_s,
                "finished_at": _now(),
            },
            indent=2,
        )
    )
    (run_dir / "verify.log").write_text(test_output)
    return result
