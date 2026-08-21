import { useState } from "react";
import { fileUrl, openSchematic } from "./api";

// Behavioral-model status badge (cycle 2). Statuses per the model contract:
// verified / compile_only / generated_unverified / failed (+ anything else
// Circuit_Builder invented gets the neutral style so it's still visible).
const MODEL_STATUS_LABELS = {
  verified: "verified",
  compile_only: "compile only",
  generated_unverified: "generated, unverified",
  failed: "failed",
};

const MODEL_TYPE_LABELS = {
  veriloga: "Verilog-A",
  verilog: "Digital Verilog",
  rnm: "RNM (SystemVerilog real)",
};

function ModelStatusBadge({ status }) {
  const cls =
    status === "verified"
      ? "ok"
      : status === "failed"
      ? "bad"
      : status === "compile_only" || status === "generated_unverified"
      ? "warn"
      : "neutral";
  return <span className={`model-status-badge ${cls}`}>{MODEL_STATUS_LABELS[status] || status || "unknown"}</span>;
}

// One requested model type: status badge, model-vs-target checks table
// (with the transistor-level SPICE measurement alongside when the same spec
// field was measured - model/SPICE disagreement is exactly what this
// feature exists to surface), download links, and inline viewers for the
// model source and its verification log.
function ModelRow({ runId, type, entry, measured }) {
  const [view, setView] = useState(null); // {name, text} currently shown inline, or null

  async function toggleView(name) {
    if (view?.name === name) {
      setView(null);
      return;
    }
    try {
      const res = await fetch(fileUrl(runId, name));
      const text = res.ok ? await res.text() : `Could not load ${name}: HTTP ${res.status}`;
      setView({ name, text });
    } catch (e) {
      setView({ name, text: `Could not load ${name}: ${String(e)}` });
    }
  }

  const checks = entry.checks && typeof entry.checks === "object" ? Object.entries(entry.checks) : [];
  const files = [
    { key: "file", label: "model source", name: entry.file },
    { key: "testbench", label: "testbench", name: entry.testbench },
    { key: "sim_log", label: "verification log", name: entry.sim_log },
  ].filter((f) => f.name);

  return (
    <div className="model-row">
      <div className="model-row-head">
        <strong>{MODEL_TYPE_LABELS[type] || type}</strong>
        <ModelStatusBadge status={entry.status} />
        {entry.file && (
          <a href={fileUrl(runId, entry.file)} target="_blank" rel="noreferrer">
            {entry.file}
          </a>
        )}
      </div>
      {entry.status_reason && <p className="model-status-reason">{entry.status_reason}</p>}
      {checks.length > 0 && (
        <table className="model-checks-table">
          <thead>
            <tr>
              <th>Spec field</th>
              <th>Target</th>
              <th>SPICE measured</th>
              <th>Model measured</th>
            </tr>
          </thead>
          <tbody>
            {checks.map(([key, c]) => (
              <tr key={key}>
                <td>{humanizeKey(key)}</td>
                <td>{c?.target ?? "n/a"}</td>
                <td>{measured && measured[key] != null ? measured[key] : "n/a"}</td>
                <td>{c?.model_measured ?? "n/a"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="model-file-actions">
        {files.map((f) => (
          <button key={f.key} type="button" className="toggle-raw" onClick={() => toggleView(f.name)}>
            {view?.name === f.name ? `Hide ${f.label}` : `View ${f.label} (${f.name})`}
          </button>
        ))}
      </div>
      {view && <pre className="raw-text model-file-view">{view.text}</pre>}
    </div>
  );
}

function Metric({ label, value, unit }) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className="metric-value">
        {value === null || value === undefined || value === "" ? (
          <span className="metric-missing">n/a</span>
        ) : (
          <>
            {value}
            {unit ? <span className="metric-unit"> {unit}</span> : null}
          </>
        )}
      </div>
    </div>
  );
}

function humanizeKey(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// summary.target_spec / summary.measured are free-form flat objects for any
// non-current-mirror topology (the specific keys vary per block - a PLL
// reports different metrics than a TIA) - render whatever keys are present
// instead of hard-coding a field list per topology.
function renderGenericMetrics(obj, prefix) {
  if (!obj || typeof obj !== "object") return null;
  return Object.entries(obj).map(([key, value]) => (
    <Metric
      key={`${prefix}-${key}`}
      label={`${humanizeKey(key)} (${prefix})`}
      value={typeof value === "number" ? Number(value.toFixed(4)) : value}
    />
  ));
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

  if (run.state === "running") {
    return (
      <div className="results-view running">
        <h2>Running Circuit_Builder...</h2>
        <p>
          This runs a real headless Circuit_Builder session: writing the
          schematic, netlisting it, rendering a PNG, and simulating the
          testbench in ngspice. It can take several minutes - watch the log
          panel for live progress.
        </p>
        <div className="spinner" />
        <p className="hint">Use the "Stop" button at the top of the page to cancel this run.</p>
      </div>
    );
  }

  const failed = run.status !== "success";
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
    <div className={`results-view ${failed ? "failed" : "success"}`}>
      <h2>{failed ? "Run failed" : "Run succeeded"}</h2>

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

      {run.library_filed && (
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

      {run.artifacts && (run.artifacts.schematic_png || run.artifacts.schematic_file) && (
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

      {isCurrentMirrorSummary ? (
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
              <ModelRow key={type} runId={run.run_id} type={type} entry={entry} measured={summary.measured} />
            ) : null
          )}
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
        {showRaw ? "Hide" : "Show"} raw Circuit_Builder reply
      </button>
      {showRaw && <pre className="raw-text">{run.raw_text}</pre>}
    </div>
  );
}
