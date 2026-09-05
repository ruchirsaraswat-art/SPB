import { useEffect, useState } from "react";
import { apiUrl, resolveResults } from "./api";
import { Metric, ModelRow, renderGenericMetrics } from "./resultsShared";
import WaveformViewer from "./WaveformViewer";

// Results sub-window: whenever a block is highlighted (same topology/blockId
// SchematicPanel already follows), show the best RESULTS the tool has for
// it - the backend resolves a filed library cell first, else the latest
// successful run, else a defined empty state (mirrors SchematicPanel exactly,
// see backend/results.py). Docked underneath SchematicPanel in the same
// right-hand column.
export default function ResultsPanel({ topology, topologyLabel, blockId }) {
  const [resolved, setResolved] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const [showSummaryJson, setShowSummaryJson] = useState(false);

  useEffect(() => {
    if (!topology) {
      setResolved(null);
      return undefined;
    }
    let stale = false;
    setLoading(true);
    setError(null);
    resolveResults(topology)
      .then((r) => {
        if (!stale) setResolved(r);
      })
      .catch((e) => {
        if (!stale) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [topology, blockId]);

  if (!topology) return null;

  const found = resolved?.found;
  const summary = resolved?.summary || {};
  const failed = found && resolved.status && resolved.status !== "success";

  const sourceLine =
    found &&
    (resolved.source === "library"
      ? `Library cell ${resolved.library}/${resolved.cell}` +
        (resolved.filed_at ? `, filed ${resolved.filed_at.slice(0, 16).replace("T", " ")}` : "") +
        (resolved.run_id ? ` (run ${resolved.run_id})` : "")
      : `Latest successful run ${resolved.run_id}` +
        (resolved.created_at ? `, ${resolved.created_at.slice(0, 16).replace("T", " ")}` : ""));

  function fileUrlFn(name) {
    return apiUrl(`${resolved.file_url_base}${encodeURIComponent(name)}`);
  }

  const artifactLinks = [
    ["netlist_file", "netlist"],
    ["testbench_file", "testbench"],
    ["testbench_sch_file", "testbench schematic"],
    ["sim_log_file", "ngspice sim log"],
    ["dc_log_file", "DC sanity-check log"],
    ["schematic_file", "schematic source"],
    ["symbol_file", "symbol"],
    ["waveform_file", "waveform (.raw)"],
  ].filter(([key]) => resolved?.artifacts?.[key]);

  return (
    <div className="schematic-panel results-panel-window">
      <div className="schematic-panel-head">
        <h3>
          Results — {topologyLabel || topology}
          {blockId ? <span className="schematic-block-id"> ({blockId})</span> : null}
        </h3>
        <button type="button" className="toggle-raw collapse-btn" onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </div>

      {!collapsed && (
        <div className="schematic-panel-body">
          {loading && <p className="hint">Looking up the latest results...</p>}
          {error && <div className="error-box small">{error}</div>}

          {!loading && !error && resolved && !found && (
            <p className="schematic-empty">{resolved.message}</p>
          )}

          {!loading && !error && found && (
            <>
              <p className={`results-status-line ${failed ? "bad" : "ok"}`}>
                {failed ? "Failed" : "Success"}
                {summary.error ? ` — ${summary.error}` : ""}
              </p>
              <p className="hint schematic-source">
                {sourceLine}
                {resolved.chosen_architecture ? ` — ${resolved.chosen_architecture}` : ""}
                {resolved.label ? ` — "${resolved.label}"` : ""}
              </p>

              <div className="metrics-grid">
                <Metric label="Topology" value={summary.topology ?? resolved.spec?.topology} />
                <Metric label="Corner" value={summary.corner ?? resolved.spec?.corner} />
                <Metric label="Temperature" value={summary.temp_c ?? resolved.spec?.temp_c} unit="C" />
                <Metric label="VDD" value={summary.vdd_v ?? resolved.spec?.vdd_v} unit="V" />
                {renderGenericMetrics(summary.target_spec, "target")}
                {renderGenericMetrics(summary.measured, "measured")}
              </div>

              {summary.models && typeof summary.models === "object" && Object.keys(summary.models).length > 0 && (
                <div className="models-block">
                  <h4>Behavioral models</h4>
                  {Object.entries(summary.models).map(([type, entry]) =>
                    entry && typeof entry === "object" ? (
                      <ModelRow key={type} fileUrlFn={fileUrlFn} type={type} entry={entry} measured={summary.measured} />
                    ) : null
                  )}
                </div>
              )}

              {(resolved.logs || []).length > 0 && (
                <div className="meas-block">
                  <h4>Simulation log measurements</h4>
                  {resolved.logs.map((log) => (
                    <div key={log.name} className="meas-log">
                      <div className="meas-log-head">
                        <a href={fileUrlFn(log.name)} target="_blank" rel="noreferrer">
                          {log.name}
                        </a>
                        {log.tokens.map((t) => (
                          <span key={t} className={`model-status-badge ${t.endsWith("PASS") ? "ok" : "bad"}`}>
                            {t}
                          </span>
                        ))}
                      </div>
                      {log.entries.length > 0 ? (
                        <table className="model-checks-table">
                          <thead>
                            <tr>
                              <th>Name</th>
                              <th>Value</th>
                            </tr>
                          </thead>
                          <tbody>
                            {log.entries.map((e, i) => (
                              <tr key={`${e.name}-${i}`}>
                                <td>{e.name}</td>
                                <td>{e.value ?? "n/a"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ) : (
                        <p className="hint">No parsed MEAS/assignment lines in this log - open it directly to read it.</p>
                      )}
                    </div>
                  ))}
                </div>
              )}

              <WaveformViewer waveform={resolved.waveform} waveformUrlBase={resolved.waveform_url_base} />

              {summary.notes && (
                <div className="notes-box">
                  <strong>Notes from Circuit_Builder:</strong> {summary.notes}
                </div>
              )}

              {artifactLinks.length > 0 && (
                <div className="artifacts-block">
                  <h4>Raw artifacts</h4>
                  <ul>
                    {artifactLinks.map(([key, label]) => (
                      <li key={key}>
                        <a href={fileUrlFn(resolved.artifacts[key])} target="_blank" rel="noreferrer">
                          {label} ({resolved.artifacts[key]})
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <button className="toggle-raw" onClick={() => setShowSummaryJson((v) => !v)}>
                {showSummaryJson ? "Hide" : "Show"} raw summary.json
              </button>
              {showSummaryJson && <pre className="raw-text">{JSON.stringify(summary, null, 2)}</pre>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
