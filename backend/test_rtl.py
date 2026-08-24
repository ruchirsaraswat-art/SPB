"""
Unit/integration tests for spec-document intake + RTL generation (2026-08-23
rtl-gen spec: backend/spec_docs.py, backend/rtl_generate.py, and the
/api/spec_docs + /api/rtl_generate endpoints). All claude invocations are
STUBBED - running this suite costs nothing; the rtl_generate honesty check
runs REAL iverilog -g2012 + vvp against stub-written fixtures, including a
deliberately-broken Verilog DUT that must FAIL server re-execution.

Run:  cd backend && .venv/bin/python3 -m pytest test_rtl.py -q
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import arch_chat
import rtl_generate as rtl_mod
import spec_docs as sd
from interfaces import normalize_interface

# ---------------------------------------------------------------------------
# Fixtures / helpers


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
            {"name": "pdata", "direction": "a2d", "width": 16, "clock_domain": "clk_a",
             "rate": "word", "rate_mhz": 100, "group": "data", "handshake": "none"},
        ],
        "cdc_points": [],
        "reset_sequence": [],
        "power_sequence": [],
    }
    base.update(overrides)
    return normalize_interface(base)


DIG_ARCH = {
    "blocks": [
        {"id": "afe-if", "label": "AFE IF", "topology": None, "kind": "iface", "col": 0, "row": 0},
        {"id": "rx-fifo", "label": "Elastic FIFO", "topology": "elastic_fifo", "col": 1, "row": 0},
        {"id": "rx-align", "label": "Word Aligner", "topology": "word_aligner", "col": 2, "row": 0},
    ],
    "edges": [{"from": "afe-if", "to": "rx-fifo"}, {"from": "rx-fifo", "to": "rx-align"}],
}

GOOD_FIELDS = {
    "rx-fifo": {"depth_words": 4, "width_bits": 8, "ppm_tolerance": 0, "skip_symbol": ""},
    "rx-align": {"parallel_width_bits": 16, "align_pattern": "K28.5",
                 "slip_granularity_bits": 1, "lock_threshold_patterns": 4,
                 "align_latency_cycles": 2},
}


def rtl_req(**overrides):
    req = {
        "scope": "block",
        "phy_type": "ser-des",
        "digital_architecture": DIG_ARCH,
        "block_id": "rx-fifo",
        "block_fields": GOOD_FIELDS,
        "interface": minimal_if(),
        "library": "rtl_test_lib",
    }
    req.update(overrides)
    return req


def poll_run(client, run_id, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["state"] == "done":
            return r
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout_s}s")


# ---------------------------------------------------------------------------
# Verilog fixtures (validated by hand with iverilog before being baked in)

RX_FIFO_V = """module rx_fifo #(parameter DEPTH_WORDS = 4, parameter WIDTH_BITS = 8) (
  input  wire                  clk,
  input  wire                  rst,
  input  wire                  push,
  input  wire [WIDTH_BITS-1:0] din,
  input  wire                  pop,
  output wire [WIDTH_BITS-1:0] dout,
  output wire                  empty,
  output wire                  full
);
  reg [WIDTH_BITS-1:0] mem [0:DEPTH_WORDS-1];
  reg [7:0] wp, rp, count;
  assign empty = (count == 0);
  assign full  = (count == DEPTH_WORDS);
  assign dout  = mem[rp];
  always @(posedge clk) begin
    if (rst) begin
      wp <= 0; rp <= 0; count <= 0;
    end else begin
      if (push && !full) begin
        mem[wp] <= din;
        wp <= (wp + 1) % DEPTH_WORDS;
      end
      if (pop && !empty) rp <= (rp + 1) % DEPTH_WORDS;
      count <= count + (push && !full) - (pop && !empty);
    end
  end
endmodule
"""

# The deliberately-broken DUT: data corrupted on the read side - it COMPILES
# fine but the TB's data_mismatch check must fire on the server re-run.
RX_FIFO_BROKEN_V = RX_FIFO_V.replace(
    "assign dout  = mem[rp];",
    "assign dout  = mem[rp] ^ 8'h01; // deliberately broken",
)

RX_FIFO_TB_SV = """module rx_fifo_tb;
  reg clk = 0, rst = 1, push = 0, pop = 0;
  reg  [7:0] din;
  wire [7:0] dout;
  wire empty, full;
  integer i;
  byte expq[$];
  rx_fifo #(.DEPTH_WORDS(4), .WIDTH_BITS(8)) dut (
    .clk(clk), .rst(rst), .push(push), .din(din),
    .pop(pop), .dout(dout), .empty(empty), .full(full));
  always #5 clk = ~clk;
  initial begin
    repeat (2) @(negedge clk); rst = 0;
    for (i = 0; i < 4; i = i + 1) begin
      @(negedge clk); push = 1; din = i * 3 + 1; expq.push_back(i * 3 + 1);
    end
    @(negedge clk); push = 0;
    if (!full) begin $display("RTL_TB_FAIL fill_to_full"); $finish; end
    for (i = 0; i < 4; i = i + 1) begin
      @(negedge clk);
      if (dout !== expq.pop_front()) begin $display("RTL_TB_FAIL data_mismatch"); $finish; end
      pop = 1; @(negedge clk); pop = 0;
    end
    if (!empty) begin $display("RTL_TB_FAIL drain_to_empty"); $finish; end
    $display("RTL_TB_PASS"); $finish;
  end
  initial begin #100000 $display("RTL_TB_FAIL watchdog_timeout"); $finish; end
endmodule
"""

RX_ALIGN_V = """module rx_align #(parameter PARALLEL_WIDTH_BITS = 16) (
  input  wire clk,
  input  wire rst,
  input  wire [PARALLEL_WIDTH_BITS-1:0] din,
  output reg  [PARALLEL_WIDTH_BITS-1:0] dout
);
  always @(posedge clk) begin
    if (rst) dout <= 0;
    else dout <= din;
  end
endmodule
"""

RX_ALIGN_TB_SV = """module rx_align_tb;
  reg clk = 0, rst = 1;
  reg  [15:0] din;
  wire [15:0] dout;
  rx_align #(.PARALLEL_WIDTH_BITS(16)) dut (.clk(clk), .rst(rst), .din(din), .dout(dout));
  always #5 clk = ~clk;
  initial begin
    repeat (2) @(negedge clk); rst = 0;
    din = 16'hBEEF; @(negedge clk);
    if (dout !== 16'hBEEF) begin $display("RTL_TB_FAIL passthrough"); $finish; end
    $display("RTL_TB_PASS"); $finish;
  end
  initial begin #100000 $display("RTL_TB_FAIL watchdog_timeout"); $finish; end
endmodule
"""

TOP_V = """module ser_des_controller_top (
  input  wire        clk_rx_word,
  input  wire        clk_ref,
  input  wire        rstn_por,
  input  wire [15:0] rx_pdata,
  output wire [15:0] core_data
);
  wire [15:0] aligned;
  rx_align #(.PARALLEL_WIDTH_BITS(16)) u_align (
    .clk(clk_rx_word), .rst(~rstn_por), .din(rx_pdata), .dout(aligned));
  assign core_data = aligned;
endmodule
"""

TOP_TB_SV = """module ser_des_controller_top_tb;
  reg clk = 0, rstn = 0;
  reg  [15:0] rx_pdata;
  wire [15:0] core_data;
  ser_des_controller_top dut (
    .clk_rx_word(clk), .clk_ref(clk), .rstn_por(rstn),
    .rx_pdata(rx_pdata), .core_data(core_data));
  always #5 clk = ~clk;
  initial begin
    repeat (2) @(negedge clk); rstn = 1;
    rx_pdata = 16'hA5A5; @(negedge clk);
    if (core_data !== 16'hA5A5) begin $display("RTL_TB_FAIL rx_datapath"); $finish; end
    $display("RTL_TB_PASS"); $finish;
  end
  initial begin #100000 $display("RTL_TB_FAIL watchdog_timeout"); $finish; end
endmodule
"""


def block_summary_entry(mod, topology, status="verified", checks=None):
    return {
        "block": mod, "topology": topology,
        "files": {"rtl": f"{mod}.v", "tb": f"{mod}_tb.sv", "sim_log": f"{mod}_sim.log"},
        "status": status, "status_reason": "",
        "checks": checks or [{"name": "fill_to_full", "result": "pass"}],
        "spec_citations": [],
    }


def stub_coder(files: dict[str, str], summary: dict):
    """Stubbed RTL_Coder 'session': writes the given files + rtl_summary.json
    + per-module sim logs into the run dir and claims success."""

    def stub(prompt, run_dir, timeout_s, cancel_event=None, **kwargs):
        run_dir = Path(run_dir)
        for name, content in files.items():
            (run_dir / name).write_text(content)
        for entry in summary.get("blocks", []) + (
            [summary["top"]] if isinstance(summary.get("top"), dict) else []
        ):
            log = entry["files"]["sim_log"]
            (run_dir / log).write_text("RTL_TB_PASS\n(agent-claimed log)\n")
        (run_dir / "rtl_summary.json").write_text(json.dumps(summary, indent=2))
        return {"ok": True, "reason": "", "cost_usd": 2.5, "raw_text": "Generated and verified."}

    return stub


# ---------------------------------------------------------------------------
# Spec-doc storage + status (banner suppression)


def doc_meta(**overrides):
    meta = {
        "title": "JESD209-4B LPDDR4",
        "standard_family": "JEDEC",
        "version": "B, Feb 2017",
        "source": {"type": "reference", "citation": "JESD209-4B section 7 (no file on hand)"},
        "user_notes": "x16 single-channel subset only.",
        "relevant_sections": [
            {"label": "Mode registers", "locator": "section 7.2, pages 45-58",
             "applies_to": ["controller", "interface"]},
        ],
        "applies_to_workspaces": ["controller", "interface", "firmware"],
    }
    meta.update(overrides)
    return meta


def test_spec_doc_metadata_only_lifecycle(client, workdir):
    # Nothing on file yet: null answer (banner would show).
    r = client.get("/api/spec_docs?phy_type=lpddr")
    assert r.status_code == 200
    assert r.json()["docs"] == []
    assert r.json()["status"]["lpddr"]["answer"] is None

    r = client.post("/api/spec_docs", json={"phy_type": "lpddr", "metadata": doc_meta()})
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["id"] == "jesd209-4b-lpddr4"  # slugified title
    assert stored["phy_type"] == "lpddr"
    # Attaching a doc records the answer (banner suppressed).
    status = client.get("/api/spec_docs?phy_type=lpddr").json()["status"]["lpddr"]
    assert status["answer"] == "attached"
    on_disk = json.loads((workdir / "spec_docs" / "lpddr" / "_status.json").read_text())
    assert on_disk["answer"] == "attached"

    # Same title again -> uniquified with -2.
    r2 = client.post("/api/spec_docs", json={"phy_type": "lpddr", "metadata": doc_meta()})
    assert r2.json()["id"] == "jesd209-4b-lpddr4-2"

    # Metadata edit (relevant_sections is the common one).
    r3 = client.put(
        "/api/spec_docs/lpddr/jesd209-4b-lpddr4",
        json={"metadata": {"relevant_sections": [
            {"label": "Training", "locator": "section 4.4, pages 120-133",
             "applies_to": ["controller", "firmware"]}]}},
    )
    assert r3.status_code == 200
    assert r3.json()["relevant_sections"][0]["label"] == "Training"

    # Delete removes the metadata.
    assert client.delete("/api/spec_docs/lpddr/jesd209-4b-lpddr4-2").status_code == 200
    ids = [d["id"] for d in client.get("/api/spec_docs?phy_type=lpddr").json()["docs"]]
    assert ids == ["jesd209-4b-lpddr4"]
    assert client.delete("/api/spec_docs/lpddr/jesd209-4b-lpddr4-2").status_code == 404


def test_spec_doc_no_spec_answer_suppresses_banner(client, workdir):
    r = client.post("/api/spec_docs/ser-des/answer", json={"answer": "no_spec"})
    assert r.status_code == 200
    assert r.json()["answer"] == "no_spec"
    status = client.get("/api/spec_docs?phy_type=ser-des").json()["status"]["ser-des"]
    assert status["answer"] == "no_spec" and status["answered_at"]


def test_spec_doc_file_copy_and_unreadable_mark(client, workdir, tmp_path):
    src = tmp_path / "my spec.pdf"
    src.write_text("%PDF-1.4 fake")
    r = client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": doc_meta(title="My PCIe Spec", standard_family="PCIe",
                             source={"type": "file", "path": str(src)}),
    })
    assert r.status_code == 200, r.text
    stored = r.json()
    assert stored["source"] == {"type": "file", "path": "spec_docs/ser-des/my-pcie-spec.pdf"}
    assert "unreadable_by_agent" not in stored
    assert (workdir / "spec_docs" / "ser-des" / "my-pcie-spec.pdf").read_text() == "%PDF-1.4 fake"

    # Unsupported extension: stored but marked unreadable_by_agent.
    src2 = tmp_path / "spec.docx"
    src2.write_text("binaryish")
    r2 = client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": doc_meta(title="Word Spec", source={"type": "file", "path": str(src2)}),
    })
    assert r2.json().get("unreadable_by_agent") is True

    # Delete removes the file copy too.
    client.delete("/api/spec_docs/ser-des/my-pcie-spec")
    assert not (workdir / "spec_docs" / "ser-des" / "my-pcie-spec.pdf").exists()


def test_spec_doc_422s_all_at_once(client, workdir, tmp_path, monkeypatch):
    r = client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": {"title": "", "source": {"type": "carrier_pigeon"},
                     "applies_to_workspaces": ["controller", "kitchen"]},
    })
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "title is required" in detail
    assert "source.type" in detail
    assert "applies_to_workspaces" in detail

    # Bad phy type.
    assert client.post(
        "/api/spec_docs", json={"phy_type": "../evil", "metadata": doc_meta()}
    ).status_code == 422
    assert client.get("/api/spec_docs?phy_type=../evil").status_code == 422

    # Nonexistent file path.
    r = client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": doc_meta(source={"type": "file", "path": str(tmp_path / "nope.pdf")}),
    })
    assert r.status_code == 422 and "does not exist" in r.json()["detail"]

    # Size cap (shrunk via monkeypatch - nobody writes 40 MB in a unit test).
    monkeypatch.setattr(sd, "MAX_FILE_BYTES", 4)
    big = tmp_path / "big.pdf"
    big.write_text("12345678")
    r = client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": doc_meta(source={"type": "file", "path": str(big)}),
    })
    assert r.status_code == 422 and "cap" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Chat-context rule: metadata + section locators only, 2000-char cap


def test_chat_context_renders_metadata_and_locators():
    block = sd.render_chat_context([doc_meta()])
    assert "JESD209-4B LPDDR4 (JEDEC, B, Feb 2017)" in block
    assert "section 7.2, pages 45-58" in block
    assert "x16 single-channel subset" in block
    assert "CANNOT read the documents in this chat" in block
    assert sd.render_chat_context([]) == "" and sd.render_chat_context(None) == ""


def test_chat_context_cap_notes_truncated_first_then_docs_dropped():
    noisy = doc_meta(user_notes="N" * 5000)
    block = sd.render_chat_context([noisy])
    assert len(block) <= sd.CHAT_CONTEXT_CAP
    assert "NNN..." in block  # notes truncated, not dropped wholesale
    assert "section 7.2" in block  # locators survive truncation

    five = [doc_meta(title=f"Doc {i}") for i in range(5)]
    block = sd.render_chat_context(five)
    assert len(block) <= sd.CHAT_CONTEXT_CAP
    assert "Doc 0" in block and "Doc 2" in block
    assert "Doc 3" not in block and "Doc 4" not in block
    assert "(2 more docs attached)" in block


def test_digital_arch_chat_carries_spec_doc_context(client, workdir, monkeypatch):
    prompts = []

    def stub(prompt, log_dir, log_stem, timeout_s=None, **kwargs):
        prompts.append(prompt)
        return arch_chat.ArchChatResult(status="success", raw_text="ok", cost_usd=0.01, duration_ms=1)

    monkeypatch.setattr(arch_chat, "run_arch_consultant", stub)
    req = {
        "session_id": "rtlsess1",
        "message": "How should the mode registers map?",
        "architecture": DIG_ARCH,
        "phy_type": "ser-des",
        "view": "digital",
        "spec_docs": [doc_meta()],
    }
    assert client.post("/api/arch_chat", json=req).status_code == 200
    assert "JESD209-4B LPDDR4" in prompts[0]
    assert "section 7.2, pages 45-58" in prompts[0]
    assert "CANNOT read the documents in this chat" in prompts[0]
    # Document CONTENTS never inlined - only metadata went in.
    assert "%PDF" not in prompts[0]


# ---------------------------------------------------------------------------
# POST /api/rtl_generate validation (422, all problems at once)


def test_rtl_validation_block_scope_all_problems_listed(client, workdir):
    bad_if = minimal_if()
    bad_if["signals"][0]["direction"] = "sideways"
    r = client.post("/api/rtl_generate", json=rtl_req(
        block_id="nope",
        block_fields={},
        interface=bad_if,
        library=None,
        spec_doc_ids=["ghost-doc"],
    ))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "unknown block_id 'nope'" in detail
    assert "interface: " in detail and "unknown direction" in detail
    assert "library is required" in detail

    # iface anchor is not designable.
    r = client.post("/api/rtl_generate", json=rtl_req(block_id="afe-if"))
    assert r.status_code == 422
    assert "non-designable" in r.json()["detail"]

    # missing block_id for scope=block
    r = client.post("/api/rtl_generate", json=rtl_req(block_id=None))
    assert r.status_code == 422 and "requires block_id" in r.json()["detail"]

    # unknown spec_doc_ids alone
    r = client.post("/api/rtl_generate", json=rtl_req(spec_doc_ids=["ghost-doc"]))
    assert r.status_code == 422 and "unknown spec_doc_ids: ghost-doc" in r.json()["detail"]


def test_rtl_validation_required_fields(client, workdir):
    # No block_fields entry at all: lists the required numeric fields.
    r = client.post("/api/rtl_generate", json=rtl_req(block_fields={}))
    assert r.status_code == 422
    assert "no block_fields entry" in r.json()["detail"]
    assert "depth_words" in r.json()["detail"]

    # Entry present but a required numeric field empty; text fields may be empty.
    fields = {"rx-fifo": {"depth_words": "", "width_bits": 8, "ppm_tolerance": 0,
                          "skip_symbol": ""}}
    r = client.post("/api/rtl_generate", json=rtl_req(block_fields=fields))
    assert r.status_code == 422
    assert "required field 'depth_words' has no value" in r.json()["detail"]

    # Non-numeric value for a numeric field.
    fields = {"rx-fifo": {"depth_words": "lots", "width_bits": 8, "ppm_tolerance": 0}}
    r = client.post("/api/rtl_generate", json=rtl_req(block_fields=fields))
    assert r.status_code == 422 and "must be numeric" in r.json()["detail"]


def test_rtl_validation_controller_scope(client, workdir):
    empty = {"blocks": [{"id": "x", "label": "X", "topology": None, "kind": "iface"}], "edges": []}
    r = client.post("/api/rtl_generate", json=rtl_req(
        scope="controller", block_id=None, digital_architecture=empty, block_fields={}))
    assert r.status_code == 422
    assert "zero designable blocks" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Prompt contract fragments


def test_rtl_prompt_carries_class_criteria_and_rules(workdir):
    arch = arch_chat.normalize_architecture(DIG_ARCH)
    included, warnings = rtl_mod.validate_rtl_request(
        "controller", "ser-des", arch, None, GOOD_FIELDS, minimal_if(), "lib", None)
    prompt = rtl_mod.build_rtl_prompt(
        "controller", "ser-des", arch, included, GOOD_FIELDS, minimal_if(),
        warnings, [], no_spec=True)
    # Per-block-class TB criteria go VERBATIM into the prompt contract.
    assert "`full` asserts on exactly the `depth_words`-th write" in prompt
    assert "for EVERY bit offset 0..`parallel_width_bits`-1" in prompt
    assert "Controller-top TB" in prompt and "port_conformance" in prompt
    # AFE-boundary port rule + stitching.
    assert "AFE-boundary ports come VERBATIM from the interface" in prompt
    assert "ser_des_controller_top" in prompt
    # Universal TB rules + honesty sentence.
    assert "RTL_TB_PASS" in prompt and "watchdog" in prompt.lower()
    assert "downgrade a false verified to failed" in prompt
    # no_spec: first-principles statement.
    assert "requirements come solely from the catalog fields" in prompt
    # Catalog field values are in.
    assert "depth_words" in prompt and "iverilog -g2012" in prompt
    # Deterministic filenames keyed by diagram block id ('-' -> '_').
    assert "rx_fifo.v" in prompt and "rx_align_tb.sv" in prompt


def test_rtl_prompt_spec_docs_selective_read(workdir, client, tmp_path):
    src = tmp_path / "ucie.pdf"
    src.write_text("%PDF fake")
    client.post("/api/spec_docs", json={
        "phy_type": "ser-des",
        "metadata": doc_meta(title="UCIe 1.1", standard_family="UCIe",
                             source={"type": "file", "path": str(src)}),
    })
    resolved = sd.resolve_docs_for_generation("ser-des", ["ucie-1-1"])
    assert resolved[0]["abs_path"].endswith("spec_docs/ser-des/ucie-1-1.pdf")
    block = rtl_mod.build_spec_docs_generation_block(resolved, no_spec=False)
    assert "max 20 pages per Read" in block
    assert "at most 10 additional pages" in block
    assert "Never read the whole document" in block
    assert "report the gap as a finding instead of guessing" in block
    assert resolved[0]["abs_path"] in block


# ---------------------------------------------------------------------------
# Honesty check: stubbed agent + REAL iverilog/vvp server re-execution


def test_rtl_block_run_pass_real_rerun_and_filing(client, workdir, monkeypatch):
    summary = {"scope": "block",
               "blocks": [block_summary_entry("rx_fifo", "elastic_fifo")],
               "findings": []}
    monkeypatch.setattr(rtl_mod, "run_rtl_coder", stub_coder(
        {"rx_fifo.v": RX_FIFO_V, "rx_fifo_tb.sv": RX_FIFO_TB_SV}, summary))
    r = client.post("/api/rtl_generate", json=rtl_req())
    assert r.status_code == 200, r.text
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "success", run.get("reason")
    assert run["deliverable"] == "rtl" and run["light_run"] is False
    assert run["cost_usd"] == 2.5
    blocks = run["summary"]["blocks"]
    assert blocks[0]["status"] == "verified"
    assert blocks[0]["checks"] == [{"name": "fill_to_full", "result": "pass"}]
    # Server re-run artifacts exist (fresh names - re-execution really happened).
    run_dir = workdir / "runs" / run["run_id"]
    assert "RTL_TB_PASS" in (run_dir / "rx_fifo_sim.server.log").read_text()
    # Filed into the library with rtl/rtl_tb/rtl_log views.
    assert run["summary"]["filed_cells"] == ["rtl_test_lib/rx_fifo"]
    cell_dir = workdir / "libraries" / "rtl_test_lib" / "rx_fifo"
    for f in ("rx_fifo.v", "rx_fifo_tb.sv", "rx_fifo_sim.log", "provenance.json"):
        assert (cell_dir / f).is_file(), f
    prov = json.loads((cell_dir / "provenance.json").read_text())
    assert prov["deliverable"] == "rtl" and prov["topology"] == "elastic_fifo"
    # ...and the library listing labels the views.
    libs = {l["name"]: l for l in client.get("/api/libraries").json()["libraries"]}
    views = {v["file"]: v["kind"] for c in libs["rtl_test_lib"]["cells"] for v in c["views"]}
    assert views["rx_fifo.v"] == "verilog"
    # Appears in the runs list like every other run.
    listed = {x["run_id"]: x for x in client.get("/api/runs").json()}
    assert listed[run["run_id"]]["deliverable"] == "rtl"


def test_rtl_block_downgrade_broken_verilog_must_fail_rerun(client, workdir, monkeypatch):
    """The deliberately-broken DUT: compiles, agent CLAIMS verified (and even
    plants a passing-looking sim log) - the server re-execution must catch the
    data corruption and downgrade to failed."""
    summary = {"scope": "block",
               "blocks": [block_summary_entry("rx_fifo", "elastic_fifo", status="verified")],
               "findings": []}
    monkeypatch.setattr(rtl_mod, "run_rtl_coder", stub_coder(
        {"rx_fifo.v": RX_FIFO_BROKEN_V, "rx_fifo_tb.sv": RX_FIFO_TB_SV}, summary))
    r = client.post("/api/rtl_generate", json=rtl_req())
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "failed"
    entry = run["summary"]["blocks"][0]
    assert entry["status"] == "failed"
    assert entry["status_reason"].startswith("claimed verified; server re-run failed:")
    assert "data_mismatch" in entry["status_reason"]
    # Nothing filed on a failed block run.
    assert run["summary"]["filed_cells"] == []
    assert not (workdir / "libraries" / "rtl_test_lib" / "rx_fifo").exists()


def test_rtl_block_missing_deliverable_fails(client, workdir, monkeypatch):
    summary = {"scope": "block",
               "blocks": [block_summary_entry("rx_fifo", "elastic_fifo")],
               "findings": []}
    monkeypatch.setattr(rtl_mod, "run_rtl_coder", stub_coder(
        {"rx_fifo.v": RX_FIFO_V}, summary))  # TB never written
    r = client.post("/api/rtl_generate", json=rtl_req())
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "failed"
    assert "rx_fifo_tb.sv" in run["reason"] and "missing on disk" in run["reason"]


def test_rtl_agent_failure_reported(client, workdir, monkeypatch):
    monkeypatch.setattr(
        rtl_mod, "run_rtl_coder",
        lambda *a, **k: {"ok": False, "reason": "claude CLI exited with code 1",
                         "cost_usd": None, "raw_text": ""},
    )
    r = client.post("/api/rtl_generate", json=rtl_req())
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "failed" and "exited with code 1" in run["reason"]


def test_rtl_controller_partial_verdict_selective_filing(client, workdir, monkeypatch):
    """scope=controller with one good block (rx_align), one broken block
    (rx_fifo) and a passing top: verdict 'partial'; the verified block AND the
    verified top are filed, the failed block is not."""
    summary = {
        "scope": "controller",
        "blocks": [
            block_summary_entry("rx_fifo", "elastic_fifo", status="verified"),
            block_summary_entry("rx_align", "word_aligner",
                                checks=[{"name": "passthrough", "result": "pass"}]),
        ],
        "top": {
            "module": "ser_des_controller_top",
            "files": {"rtl": "ser_des_controller_top.v",
                      "tb": "ser_des_controller_top_tb.sv",
                      "sim_log": "ser_des_controller_top_sim.log"},
            "status": "verified", "status_reason": "",
            "checks": [{"name": "rx_datapath", "result": "pass"}],
        },
        "findings": ["edge rx-fifo->rx-align has no named signal contract in the interface"],
    }
    monkeypatch.setattr(rtl_mod, "run_rtl_coder", stub_coder(
        {
            "rx_fifo.v": RX_FIFO_BROKEN_V, "rx_fifo_tb.sv": RX_FIFO_TB_SV,
            "rx_align.v": RX_ALIGN_V, "rx_align_tb.sv": RX_ALIGN_TB_SV,
            "ser_des_controller_top.v": TOP_V,
            "ser_des_controller_top_tb.sv": TOP_TB_SV,
        }, summary))
    r = client.post("/api/rtl_generate", json=rtl_req(scope="controller", block_id=None))
    assert r.status_code == 200, r.text
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "partial", run.get("reason")
    assert "rx_fifo" in run["reason"] and "still filed" in run["reason"]
    statuses = {e["block"]: e["status"] for e in run["summary"]["blocks"]}
    assert statuses == {"rx_fifo": "failed", "rx_align": "verified"}
    assert run["summary"]["top"]["status"] == "verified"
    assert run["summary"]["findings"] == summary["findings"]
    # Selective filing: verified block + top, not the failed block.
    assert sorted(run["summary"]["filed_cells"]) == [
        "rtl_test_lib/rx_align", "rtl_test_lib/ser_des_controller_top"]
    assert not (workdir / "libraries" / "rtl_test_lib" / "rx_fifo").exists()
    assert (workdir / "libraries" / "rtl_test_lib" / "ser_des_controller_top"
            / "ser_des_controller_top.v").is_file()


def test_rtl_controller_all_verified_is_success(client, workdir, monkeypatch):
    summary = {
        "scope": "controller",
        "blocks": [
            block_summary_entry("rx_fifo", "elastic_fifo"),
            block_summary_entry("rx_align", "word_aligner",
                                checks=[{"name": "passthrough", "result": "pass"}]),
        ],
        "top": {
            "module": "ser_des_controller_top",
            "files": {"rtl": "ser_des_controller_top.v",
                      "tb": "ser_des_controller_top_tb.sv",
                      "sim_log": "ser_des_controller_top_sim.log"},
            "status": "verified", "status_reason": "", "checks": [],
        },
        "findings": [],
    }
    monkeypatch.setattr(rtl_mod, "run_rtl_coder", stub_coder(
        {
            "rx_fifo.v": RX_FIFO_V, "rx_fifo_tb.sv": RX_FIFO_TB_SV,
            "rx_align.v": RX_ALIGN_V, "rx_align_tb.sv": RX_ALIGN_TB_SV,
            "ser_des_controller_top.v": TOP_V,
            "ser_des_controller_top_tb.sv": TOP_TB_SV,
        }, summary))
    r = client.post("/api/rtl_generate", json=rtl_req(scope="controller", block_id=None))
    run = poll_run(client, r.json()["run_id"])
    assert run["status"] == "success", run.get("reason")
    assert sorted(run["summary"]["filed_cells"]) == [
        "rtl_test_lib/rx_align", "rtl_test_lib/rx_fifo",
        "rtl_test_lib/ser_des_controller_top"]


def test_rtl_timeouts_per_scope():
    assert rtl_mod.rtl_timeout_for("block") == 1800
    assert rtl_mod.rtl_timeout_for("controller") == 5400
