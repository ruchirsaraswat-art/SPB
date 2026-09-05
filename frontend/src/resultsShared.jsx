import { useState } from "react";

// Presentation pieces shared between ResultsView (the just-submitted run,
// always run-file-scoped) and ResultsPanel (the results sub-window - a
// resolved run OR a filed library cell, so file links come from whichever
// base URL the caller resolved) - split out of ResultsView.jsx so the
// results sub-window doesn't fork this rendering, it reuses it.

// Behavioral-model status badge. Statuses per the model contract: verified /
// compile_only / generated_unverified / failed (+ anything else
// Circuit_Builder invented gets the neutral style so it's still visible).
export const MODEL_STATUS_LABELS = {
  verified: "verified",
  compile_only: "compile only",
  generated_unverified: "generated, unverified",
  failed: "failed",
};

export const MODEL_TYPE_LABELS = {
  veriloga: "Verilog-A",
  verilog: "Digital Verilog",
  rnm: "RNM (SystemVerilog real)",
};

export function ModelStatusBadge({ status }) {
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

export function humanizeKey(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function Metric({ label, value, unit }) {
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

// summary.target_spec / summary.measured are free-form flat objects for any
// non-current-mirror topology (the specific keys vary per block - a PLL
// reports different metrics than a TIA) - render whatever keys are present
// instead of hard-coding a field list per topology.
export function renderGenericMetrics(obj, prefix) {
  if (!obj || typeof obj !== "object") return null;
  return Object.entries(obj).map(([key, value]) => (
    <Metric
      key={`${prefix}-${key}`}
      label={`${humanizeKey(key)} (${prefix})`}
      value={typeof value === "number" ? Number(value.toFixed(4)) : value}
    />
  ));
}

// One requested model type: status badge, model-vs-target checks table
// (with the transistor-level SPICE measurement alongside when the same spec
// field was measured - model/SPICE disagreement is exactly what this
// feature exists to surface), download links, and inline viewers for the
// model source and its verification log. `fileUrlFn(name)` resolves a
// filename to a fetchable URL - a run's /api/runs/{id}/file/ or a filed
// cell's /api/libraries/{lib}/{cell}/file/, whichever the caller resolved.
export function ModelRow({ fileUrlFn, type, entry, measured }) {
  const [view, setView] = useState(null); // {name, text} currently shown inline, or null

  async function toggleView(name) {
    if (view?.name === name) {
      setView(null);
      return;
    }
    try {
      const res = await fetch(fileUrlFn(name));
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
          <a href={fileUrlFn(entry.file)} target="_blank" rel="noreferrer">
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
