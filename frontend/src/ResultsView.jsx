import { useState } from "react";
import { fileUrl, openSchematic } from "./api";
import { Metric, ModelRow, ModelStatusBadge, renderGenericMetrics } from "./resultsShared";

// Single-deliverable run modes (RunSpec.deliverable): what to call them and
// which result sections make sense. "full" renders everything, as before.
const DELIVERABLE_LABELS = {
  schematic_only: "Schematic only",
  symbol: "Symbol only",
  veriloga: "Verilog-A model only",
  verilog: "Digital Verilog model only",
  rnm: "RNM model only",
  arch_model: "Architecture model",
  arch_stitch: "Stitched top-level schematic",
};
const MODEL_ONLY_DELIVERABLES = ["veriloga", "verilog", "rnm"];

// RTL runs (2026-08-23 rtl-gen spec): per-block verdict rows with the
// SERVER re-verification result (a downgrade of the agent's claim is shown
// prominently), checks, file links, spec-citation chips; controller runs add
// the top row first, the findings list verbatim, and the filed-cell links.
function RtlBlockRow({ runId, entry, isTop, portNote }) {
  const name = isTop ? entry.module : entry.block;
  const files = entry.files || {};
  const checks = entry.checks || [];
  const verified = entry.status === "verified";
  return (
    <div className="rtl-result-row">
      <div className="rtl-result-head">
        <strong>{isTop ? `top: ${name}` : name}</strong>
        {!isTop && entry.topology && <code>({entry.topology})</code>}
        <span className={`model-status-badge ${verified ? "ok" : "bad"}`}>
          {verified ? "verified" : "failed"}
        </span>
        {[
          { label: "rtl", name: files.rtl },
          { label: "testbench", name: files.tb },
          { label: "sim log", name: files.sim_log },
        ]
          .filter((f) => f.name)
          .map((f) => (
            <a key={f.label} href={fileUrl(runId, f.name)} target="_blank" rel="noreferrer">
              {f.name}
            </a>
          ))}
      </div>
      {portNote && <p className="hint">{portNote}</p>}
      {entry.status_reason && (
        <p className="model-status-reason rtl-downgrade">{entry.status_reason}</p>
      )}
      {checks.length > 0 && (
        <p className="rtl-checks">
          {checks.map((c) => (
            <span key={c.name} className={`rtl-check ${c.result === "pass" ? "ok" : "bad"}`}>
              {c.name}: {c.result}
            </span>
          ))}
        </p>
      )}
      {(entry.spec_citations || []).length > 0 && (
        <p className="rtl-citations">
          Spec citations:{" "}
          {entry.spec_citations.map((s) => (
            <span key={s} className="spec-chip">
              {s}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function RtlSection({ run }) {
  const summary = run.summary || {};
  const ifSignals = (run.spec?.interface?.signals || []).length;
  return (
    <div className="rtl-section">
      <h3>RTL</h3>
      {summary.top && (
        <RtlBlockRow
          runId={run.run_id}
          entry={summary.top}
          isTop
          portNote={
            ifSignals
              ? `AFE-boundary port contract: all ${ifSignals} interface signals appear as top-level ports verbatim (checked by the port_conformance compile).`
              : null
          }
        />
      )}
      {(summary.blocks || []).map((entry) => (
        <RtlBlockRow key={entry.block} runId={run.run_id} entry={entry} />
      ))}
      {(summary.findings || []).length > 0 && (
        <div className="warnings-box rtl-findings">
          <strong>Findings reported by RTL_Coder (verbatim):</strong>
          <ul>
            {summary.findings.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
        </div>
      )}
      {(summary.filed_cells || []).length > 0 && (
        <p className="rtl-filed">
          Filed into library cells (views rtl / rtl_tb / rtl_log):{" "}
          {summary.filed_cells.map((c) => (
            <code key={c}>{c}</code>
          ))}
        </p>
      )}
    </div>
  );
}

export default function ResultsView({ run }) {
  const [showRaw, setShowRaw] = useState(false);
  const [xschemStatus, setXschemStatus] = useState(null);
  const [xschemBusy, setXschemBusy] = useState(false);

  if (!run) return null;

  async function handleOpenXschem() {
    setXschemBusy(true);
    setXschemStatus(null);
    try {
      const res = await openSchematic(run.run_id);
      setXschemStatus(`Launched xschem on DISPLAY=${res.display}. Look for a new window on this machine's desktop.`);
    } catch (e) {
      setXschemStatus(`Failed to launch xschem: ${String(e)}`);
    } finally {
      setXschemBusy(false);
    }
  }

  const deliverable = run.deliverable || run.spec?.deliverable || "full";
  const isRtl = deliverable === "rtl";
  const rtlScope = run.summary?.scope || run.spec?.scope;
  const deliverableLabel = isRtl
    ? `RTL - ${rtlScope === "controller" ? "controller" : "block"}`
    : DELIVERABLE_LABELS[deliverable] || null;
  const isModelOnly = MODEL_ONLY_DELIVERABLES.includes(deliverable);
  const isSymbolOnly = deliverable === "symbol";
  const isArchModel = deliverable === "arch_model";
  const isArchStitch = deliverable === "arch_stitch";
  const lightRun = run.light_run || isModelOnly || isSymbolOnly;

  if (run.state === "running") {
    return (
      <div className="results-view running">
        <h2>
          {isRtl ? "Running RTL_Coder..." : "Running Circuit_Builder..."}
          {deliverableLabel && (
            <span className="deliverable-badge">
              {deliverableLabel}
              {lightRun ? " · light run" : ""}
            </span>
          )}
        </h2>
        <p>
          {isRtl
            ? rtlScope === "controller"
              ? "This is the most expensive run type in this tool: RTL_Coder generates and verifies every designable block plus the stitched controller top (60-120 min typical). The verdict is server-enforced - the backend re-runs iverilog + vvp itself per block and the top."
              : "RTL_Coder is generating this block's Verilog + self-checking testbench and verifying it with iverilog (up to ~30 min). The verdict is server-enforced - the backend re-runs the compile + simulation itself."
            : isArchStitch
            ? "This is a stitch run: Circuit_Builder places each block's filed library symbol into one top-level xschem schematic, wires them per the drawn connections, netlists it and renders a PNG. Block internals are untouched and nothing is simulated."
            : isArchModel
            ? "This is an architecture-model run: Circuit_Builder authors one behavioral model per included diagram block plus a top-level module wired per the drawn connections, then verifies the chain end to end with a self-checking testbench (iverilog). No transistor-level design."
            : isSymbolOnly
            ? "This is a cheap symbol-only run: Circuit_Builder writes just the xschem .sym for this block and smoke-checks it by netlisting a tiny wrapper schematic. It should finish in a few minutes."
            : isModelOnly
            ? "This is a model-only run: Circuit_Builder authors just the requested behavioral model from the spec and verifies it with its self-checking testbench (no transistor-level design). Usually much faster than a full run."
            : deliverable === "schematic_only"
            ? "This is a schematic-only run: Circuit_Builder designs the circuit, writes the xschem schematic, netlists it and renders a PNG - skipping the verification simulation suite. Watch the log panel for live progress."
            : "This runs a real headless Circuit_Builder session: writing the schematic, netlisting it, rendering a PNG, and simulating the testbench in ngspice. It can take several minutes - watch the log panel for live progress."}
        </p>
        <div className="spinner" />
        <p className="hint">Use the "Stop" button at the top of the page to cancel this run.</p>
      </div>
    );
  }

  // "partial" (controller RTL runs only): top or >= 1 block failed but >= 1
  // block verified - the verified blocks were still filed. Amber, not red.
  const partial = run.status === "partial";
  const failed = run.status !== "success" && !partial;
  const summary = run.summary || {};
  // The spec as submitted - the authoritative record of the run conditions
  // (corner, temperature, VDD) that were REQUESTED, whether or not
  // Circuit_Builder echoed them back into summary.json.
  const spec = run.spec || {};
  // Soft sky130-feasibility warnings that were active when this spec was
  // submitted, persisted into the run record by the backend at submit time -
  // shown here so a result viewed later still carries its "this spec was
  // aggressive for sky130" context.
  const warnings = run.warnings || [];
  // Only the current-mirror flow's summary.json has this key - use it to
  // decide whether to render the hand-tuned mirror-specific metric layout
  // or the generic target_spec/measured layout every other topology uses.
  const isCurrentMirrorSummary = summary.iref_target_ua !== undefined;

  return (
    <div className={`results-view ${failed ? "failed" : partial ? "partial" : "success"}`}>
      <h2>
        {failed ? "Run failed" : partial ? "Run partially succeeded" : "Run succeeded"}
        {deliverableLabel && (
          <span className="deliverable-badge">
            {deliverableLabel}
            {lightRun ? " · light run" : ""}
          </span>
        )}
        {partial && <span className="deliverable-badge partial-badge">partial</span>}
      </h2>

      {partial && (
        <div className="warnings-box">
          <strong>Partial result:</strong> {run.reason}
        </div>
      )}

      {failed && (
        <div className="error-box">
          <strong>Reason:</strong> {run.reason || "Unknown failure"}
          {summary.error ? (
            <>
              <br />
              <strong>Circuit_Builder reported:</strong> {summary.error}
            </>
          ) : null}
        </div>
      )}

      {isRtl && <RtlSection run={run} />}

      {!isRtl && run.library_filed && (
        <div className="notes-box library-filed">
          <strong>Filed into design library:</strong> {run.library_filed.library} /{" "}
          {run.library_filed.cell} ({run.library_filed.views?.length ?? 0} views - schematic,
          netlist, testbench, models, measurements). Browsable from xschem as{" "}
          <code>
            {run.library_filed.library}/{run.library_filed.cell}
          </code>
          .
        </div>
      )}
      {run.library_error && (
        <div className="warnings-box">
          <strong>Library filing problem:</strong> {run.library_error} (the run itself succeeded -
          all artifacts are still in the run directory below)
        </div>
      )}

      {warnings.length > 0 && (
        <div className="warnings-box">
          <strong>Feasibility warnings active when this spec was submitted:</strong>
          <ul>
            {warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {isSymbolOnly && (
        <div className="symbol-block">
          <h3>Symbol</h3>
          {run.artifacts?.symbol_file ? (
            <p>
              <a href={fileUrl(run.run_id, run.artifacts.symbol_file)} target="_blank" rel="noreferrer">
                {run.artifacts.symbol_file}
              </a>{" "}
              {summary.cell ? <>— cell <code>{summary.cell}</code></> : null}
            </p>
          ) : (
            !failed && <p className="hint">No symbol file was recorded for this run.</p>
          )}
          {Array.isArray(summary.ports) && summary.ports.length > 0 && (
            <p>
              <strong>Ports:</strong> <code>{summary.ports.join(", ")}</code>
            </p>
          )}
          {summary.port_source && (
            <p className="hint">
              Pin list derived from{" "}
              {summary.port_source === "filed_schematic"
                ? "the filed library schematic"
                : "the spec (no filed schematic existed for this block)"}
              .
            </p>
          )}
        </div>
      )}

      {isArchModel && (
        <div className="symbol-block arch-model-block">
          <h3>Architecture model</h3>
          <p>
            <strong>Top module:</strong> <code>{summary.cell || "n/a"}</code>{" "}
            <ModelStatusBadge status={summary.model_status} />{" "}
            <span className="hint">({summary.model_type === "verilog" ? "digital Verilog" : "RNM"})</span>
          </p>
          {summary.status_reason && <p className="model-status-reason">{summary.status_reason}</p>}
          {Array.isArray(summary.blocks_included) && (
            <p>
              <strong>Blocks modeled:</strong> {summary.blocks_included.join(", ")}
            </p>
          )}
          <ul className="arch-model-files">
            {summary.top_file && (
              <li>
                <a href={fileUrl(run.run_id, summary.top_file)} target="_blank" rel="noreferrer">
                  top-level wiring module ({summary.top_file})
                </a>
              </li>
            )}
            {Object.entries(summary.block_files || {}).map(([blockId, fname]) => (
              <li key={blockId}>
                <a href={fileUrl(run.run_id, fname)} target="_blank" rel="noreferrer">
                  {blockId} model ({fname})
                </a>
              </li>
            ))}
            {summary.testbench_file && (
              <li>
                <a href={fileUrl(run.run_id, summary.testbench_file)} target="_blank" rel="noreferrer">
                  end-to-end testbench ({summary.testbench_file})
                </a>
              </li>
            )}
            {summary.sim_log_file && (
              <li>
                <a href={fileUrl(run.run_id, summary.sim_log_file)} target="_blank" rel="noreferrer">
                  verification log ({summary.sim_log_file})
                </a>
              </li>
            )}
          </ul>
        </div>
      )}

      {isArchStitch && summary.blocks_instantiated && (
        <div className="symbol-block">
          <h3>Blocks stitched</h3>
          <ul className="arch-model-files">
            {Object.entries(summary.blocks_instantiated).map(([blockId, ref]) => (
              <li key={blockId}>
                {blockId} — placed from <code>{ref}</code>
              </li>
            ))}
          </ul>
          {Array.isArray(summary.top_ports) && summary.top_ports.length > 0 && (
            <p>
              <strong>Top-level ports:</strong> <code>{summary.top_ports.join(", ")}</code>
            </p>
          )}
          {summary.symbols_generated && Object.keys(summary.symbols_generated).length > 0 && (
            <p>
              <strong>Symbols generated during this stitch:</strong>{" "}
              {Object.values(summary.symbols_generated).map((fname, i) => (
                <span key={fname}>
                  {i > 0 ? ", " : ""}
                  <a href={fileUrl(run.run_id, fname)} target="_blank" rel="noreferrer">
                    {fname}
                  </a>
                </span>
              ))}
              {Array.isArray(run.symbols_filed) && run.symbols_filed.length > 0 ? (
                <span className="hint"> — filed back into: {run.symbols_filed.join(", ")}</span>
              ) : (
                <span className="hint"> — not filed back into the block cells (check the run log)</span>
              )}
            </p>
          )}
        </div>
      )}

      {!isSymbolOnly && !isModelOnly && !isArchModel && run.artifacts && (run.artifacts.schematic_png || run.artifacts.schematic_file) && (
        <div className="schematic-block">
          <h3>Schematic</h3>
          {run.artifacts.schematic_png ? (
            <img
              className="schematic-img"
              src={fileUrl(run.run_id, run.artifacts.schematic_png)}
              alt="Rendered schematic"
            />
          ) : (
            <p className="hint">
              No rendered PNG was produced for this run ({run.artifacts.schematic_file} exists but wasn't
              rendered) - you can still open the schematic itself below.
            </p>
          )}
          <div>
            <p>Would you like to see the design in xschem?</p>
            <button onClick={handleOpenXschem} disabled={xschemBusy}>
              {xschemBusy ? "Launching..." : "Yes, open in xschem (GUI)"}
            </button>
            {xschemStatus && <p className="hint">{xschemStatus}</p>}
          </div>
        </div>
      )}

      {isSymbolOnly || isArchModel || isArchStitch || isRtl ? null : isModelOnly ? (
        // Model-only run: conditions + targets only (the models table below
        // carries target vs model-measured per check) - there is no SPICE
        // "measured" side to render.
        <div className="metrics-grid">
          <Metric label="Topology" value={summary.topology ?? spec.topology} />
          <Metric label="Corner" value={summary.corner ?? spec.corner} />
          <Metric label="Temperature" value={summary.temp_c ?? spec.temp_c} unit="C" />
          <Metric label="VDD" value={summary.vdd_v ?? spec.vdd_v} unit="V" />
          {renderGenericMetrics(summary.target_spec, "target")}
        </div>
      ) : isCurrentMirrorSummary ? (
        <div className="metrics-grid">
          <Metric label="Topology" value={summary.topology} />
          <Metric label="Corner" value={summary.corner ?? spec.corner} />
          <Metric label="Temperature" value={summary.temp_c ?? spec.temp_c} unit="C" />
          <Metric label="VDD" value={summary.vdd_v ?? spec.vdd_v} unit="V" />
          <Metric label="Iref target" value={summary.iref_target_ua} unit="uA" />
          <Metric label="Iref simulated" value={summary.iref_simulated_ua} unit="uA" />
          <Metric label="Iout simulated" value={summary.iout_simulated_ua} unit="uA" />
          <Metric label="Ratio target" value={summary.ratio_target} />
          <Metric
            label="Ratio measured"
            value={
              typeof summary.ratio_measured === "number"
                ? summary.ratio_measured.toFixed(4)
                : summary.ratio_measured
            }
          />
          <Metric label="W ref" value={summary.w_ref_um} unit="um" />
          <Metric label="L ref" value={summary.l_ref_um} unit="um" />
          <Metric label="W mirror" value={summary.w_mirror_um} unit="um" />
          <Metric label="Vds (mirror, headroom)" value={summary.vds_mirror_v} unit="V" />
          <Metric label="Vgs (reference)" value={summary.vgs_ref_v} unit="V" />
        </div>
      ) : (
        // Rendered even when summary is empty (e.g. a failed run): the run
        // conditions (topology/corner/temperature/VDD) come from the
        // submitted spec, which always exists on the run record.
        <div className="metrics-grid">
          <Metric label="Topology" value={summary.topology ?? spec.topology} />
          <Metric label="Topology choice" value={summary.topology_choice} />
          <Metric label="Corner" value={summary.corner ?? spec.corner} />
          <Metric label="Temperature" value={summary.temp_c ?? spec.temp_c} unit="C" />
          <Metric label="VDD" value={summary.vdd_v ?? spec.vdd_v} unit="V" />
          {renderGenericMetrics(summary.target_spec, "target")}
          {renderGenericMetrics(summary.measured, "measured")}
        </div>
      )}

      {summary.models && typeof summary.models === "object" && Object.keys(summary.models).length > 0 && (
        <div className="models-block">
          <h3>Behavioral models</h3>
          {Object.entries(summary.models).map(([type, entry]) =>
            entry && typeof entry === "object" ? (
              <ModelRow
                key={type}
                fileUrlFn={(name) => fileUrl(run.run_id, name)}
                type={type}
                entry={entry}
                measured={summary.measured}
              />
            ) : null
          )}
        </div>
      )}

      {summary.dc_check_note && (
        <div className="notes-box">
          <strong>DC sanity check:</strong> {summary.dc_check_note}
        </div>
      )}
      {summary.notes && (
        <div className="notes-box">
          <strong>Notes from Circuit_Builder:</strong> {summary.notes}
        </div>
      )}
      {summary.sizing_note && (
        <div className="notes-box">
          <strong>Sizing note:</strong> {summary.sizing_note}
        </div>
      )}

      <div className="artifacts-block">
        <h3>Raw artifacts</h3>
        <ul>
          {run.artifacts?.netlist_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.netlist_file)} target="_blank" rel="noreferrer">
                netlist ({run.artifacts.netlist_file})
              </a>
            </li>
          )}
          {run.artifacts?.testbench_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.testbench_file)} target="_blank" rel="noreferrer">
                testbench ({run.artifacts.testbench_file})
              </a>
            </li>
          )}
          {run.artifacts?.testbench_sch_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.testbench_sch_file)} target="_blank" rel="noreferrer">
                testbench schematic ({run.artifacts.testbench_sch_file})
              </a>
            </li>
          )}
          {run.artifacts?.symbol_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.symbol_file)} target="_blank" rel="noreferrer">
                symbol ({run.artifacts.symbol_file})
              </a>
            </li>
          )}
          {run.artifacts?.dc_log_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.dc_log_file)} target="_blank" rel="noreferrer">
                DC sanity-check log ({run.artifacts.dc_log_file})
              </a>
            </li>
          )}
          {run.artifacts?.sim_log_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.sim_log_file)} target="_blank" rel="noreferrer">
                ngspice sim log ({run.artifacts.sim_log_file})
              </a>
            </li>
          )}
          {run.artifacts?.schematic_file && (
            <li>
              <a href={fileUrl(run.run_id, run.artifacts.schematic_file)} target="_blank" rel="noreferrer">
                schematic source ({run.artifacts.schematic_file})
              </a>
            </li>
          )}
          <li>
            <a href={fileUrl(run.run_id, "session.log")} target="_blank" rel="noreferrer">
              full Circuit_Builder session log (session.log)
            </a>
          </li>
          <li>
            <a href={fileUrl(run.run_id, "claude_stream.jsonl")} target="_blank" rel="noreferrer">
              raw event stream (claude_stream.jsonl)
            </a>
          </li>
        </ul>
      </div>

      <button className="toggle-raw" onClick={() => setShowRaw((v) => !v)}>
        {showRaw ? "Hide" : "Show"} raw {isRtl ? "RTL_Coder" : "Circuit_Builder"} reply
      </button>
      {showRaw && <pre className="raw-text">{run.raw_text}</pre>}
    </div>
  );
}
