import { useCallback, useEffect, useRef, useState } from "react";
import { fetchChannelFile, getChannel, importChannel, listChannels } from "./api";

// DDR channel models, inside the AFE workspace (2026-08-29 channel-artifact
// spec). One imported Touchstone file (.s2p / .s4p) becomes two artifacts
// from the SAME fitted network - a passivity-gated SPICE subcircuit for
// transistor-level decks and UI-spaced pulse-response cursors for RNM
// testbenches - so a link measured either way sees the same channel.
//
// Fitting takes tens of seconds to minutes, so the import is a background job
// on the backend and this panel polls it, showing the live fit log line. An
// unchanged file is never refitted (the backend keys its cache on file content
// + fit parameters), so re-selecting a file comes back instantly.
//
// The number this panel exists to show is the PASSIVITY VERDICT: an
// rms-optimal but non-passive fit looks perfect in-band and then blows a
// transient up, so max-singular-value is displayed as prominently as loss.

const ARTIFACT_VIEWS = [
  { name: "channel.sp", label: "SPICE subckt" },
  { name: "cursors.txt", label: "Cursors" },
  { name: "taps.hex", label: "Taps (Q16.16)" },
  { name: "fit.log", label: "Fit log" },
];

function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

function fmtGHz(hz) {
  const g = hz / 1e9;
  return g >= 1 ? `${g.toFixed(2)} GHz` : `${(hz / 1e6).toFixed(0)} MHz`;
}

function fmtDb(v) {
  return typeof v === "number" ? `${v.toFixed(2)} dB` : "-";
}

function PassivityVerdict({ metrics }) {
  const sv = metrics.max_singular_value;
  const limit = metrics.passivity_limit;
  return (
    <div className={`chan-verdict ${metrics.passive ? "chan-verdict-ok" : "chan-verdict-bad"}`}>
      <strong>{metrics.passive ? "Passive" : "NOT passive"}</strong>
      <span>
        max singular value {sv?.toFixed(4)} (limit {limit}) at{" "}
        {fmtGHz(metrics.max_singular_value_freq_hz)}
      </span>
      <span className="hint">
        Checked over 1 MHz - 5 THz, not just in band: a model with gain outside the
        fit band is what blows a transient up.
      </span>
    </div>
  );
}

function Metrics({ channel }) {
  const m = channel.metrics || {};
  const cur = m.cursors || {};
  return (
    <div className="chan-metrics">
      <PassivityVerdict metrics={m} />

      <div className="chan-metric-grid">
        <div>
          <span className="chan-metric-label">Ports</span>
          <span>{m.nports}-port ({m.subckt_name})</span>
        </div>
        <div>
          <span className="chan-metric-label">Fit</span>
          <span>
            {m.n_poles_real}r + {m.n_poles_cmplx}c poles, data to {fmtGHz(m.fit_bandwidth_hz)}
          </span>
        </div>
        <div>
          <span className="chan-metric-label">Fit rms error</span>
          <span>{m.rms_error?.toFixed(5)}</span>
        </div>
        <div>
          <span className="chan-metric-label">Data</span>
          <span>
            {m.n_freq_points} pts, {fmtGHz(m.freq_min_hz)} - {fmtGHz(m.freq_max_hz)},{" "}
            {m.reference_z?.toFixed(0)} ohm ref
          </span>
        </div>
        <div>
          <span className="chan-metric-label">State resistors</span>
          <span>
            {m.state_resistor_min_ohm?.toPrecision(3)} - {m.state_resistor_max_ohm?.toPrecision(3)} ohm
          </span>
        </div>
      </div>

      <h4>Insertion loss</h4>
      <table className="chan-table">
        <thead>
          <tr>
            <th>Freq</th>
            <th>S21 data</th>
            <th>S21 model</th>
            <th>Error</th>
            {m.nports === 4 && <th>NEXT</th>}
            {m.nports === 4 && <th>FEXT</th>}
          </tr>
        </thead>
        <tbody>
          {(m.insertion_loss || []).map((row) => (
            <tr key={row.freq_hz}>
              <td>{fmtGHz(row.freq_hz)}</td>
              <td>{fmtDb(row.data_db)}</td>
              <td>{fmtDb(row.fit_db)}</td>
              <td>{row.error_db >= 0 ? "+" : ""}{row.error_db?.toFixed(3)} dB</td>
              {m.nports === 4 && <td>{fmtDb(row.next_data_db)}</td>}
              {m.nports === 4 && <td>{fmtDb(row.fext_data_db)}</td>}
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Pulse-response cursors ({cur.ui_ps} ps UI)</h4>
      <div className="chan-metric-grid">
        <div>
          <span className="chan-metric-label">h0 (main cursor)</span>
          <span>{cur.h0?.toFixed(5)}</span>
        </div>
        <div>
          <span className="chan-metric-label">sum |ISI|</span>
          <span>{cur.sum_abs_isi?.toFixed(5)}</span>
        </div>
        <div>
          <span className="chan-metric-label">Worst-case eye</span>
          <span>{cur.worst_case_eye?.toFixed(5)} of full swing</span>
        </div>
        <div>
          <span className="chan-metric-label">First precursor / postcursor</span>
          <span>
            {cur.precursors?.length ? cur.precursors[cur.precursors.length - 1].toFixed(5) : "-"} /{" "}
            {cur.postcursors?.length ? cur.postcursors[0].toFixed(5) : "-"}
          </span>
        </div>
      </div>
      <p className="hint">
        {cur.cursors?.length} UI-spaced taps ({cur.precursors?.length} pre, {cur.postcursors?.length} post),
        terminated in a 50 ohm driver and 50 ohm ODT - the same channel the SPICE subckt
        above represents.
      </p>
    </div>
  );
}

function ArtifactViewer({ channelId }) {
  const [view, setView] = useState(null);
  const [text, setText] = useState("");
  const [error, setError] = useState(null);

  useEffect(() => {
    setView(null);
    setText("");
    setError(null);
  }, [channelId]);

  const open = async (name) => {
    if (view === name) {
      setView(null);
      return;
    }
    setView(name);
    setText("");
    setError(null);
    try {
      setText(await fetchChannelFile(channelId, name));
    } catch (e) {
      setError(errText(e));
    }
  };

  return (
    <div className="chan-artifacts">
      <div className="chan-artifact-buttons">
        {ARTIFACT_VIEWS.map((a) => (
          <button
            key={a.name}
            type="button"
            className="toggle-raw"
            onClick={() => open(a.name)}
          >
            {view === a.name ? `Hide ${a.label.toLowerCase()}` : a.label}
          </button>
        ))}
      </div>
      {error && <div className="error-box">{error}</div>}
      {view && !error && (
        <pre className="chan-raw">{text || "loading..."}</pre>
      )}
    </div>
  );
}

export default function ChannelPanel() {
  const [channels, setChannels] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pathValue, setPathValue] = useState("");
  const [open, setOpen] = useState(false);
  const fileRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      setChannels(await listChannels());
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll the selected channel while its fit is running (the backend rewrites
  // meta.json on every fit step, so `progress` is a live log line).
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return undefined;
    }
    let alive = true;
    let timer = null;
    const tick = async () => {
      try {
        const body = await getChannel(selectedId);
        if (!alive) return;
        setDetail(body);
        if (body.state === "running") {
          timer = setTimeout(tick, 1500);
        } else {
          refresh();
        }
      } catch (e) {
        if (alive) setError(errText(e));
      }
    };
    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [selectedId, refresh]);

  const startImport = async (payload) => {
    setError(null);
    setBusy(true);
    try {
      const res = await importChannel(payload);
      setSelectedId(res.channel_id);
      setOpen(true);
      await refresh();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const content = await file.text();
    await startImport({ content, filename: file.name, name: file.name.replace(/\.[^.]+$/, "") });
    if (fileRef.current) fileRef.current.value = "";
  };

  const handlePath = async () => {
    const path = pathValue.trim();
    if (!path) return;
    await startImport({ path });
    setPathValue("");
  };

  const running = detail?.state === "running";
  const failed = detail?.state === "done" && detail?.status === "failed";

  return (
    <div className="chan-section">
      <div className="chan-head">
        <h3>DDR channel model</h3>
        <button type="button" className="toggle-raw" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : `Show${channels.length ? ` (${channels.length})` : ""}`}
        </button>
      </div>
      <p className="hint">
        Import a measured/simulated channel (.s2p through path, .s4p with a crosstalk
        aggressor). One import emits both a passivity-gated SPICE subcircuit for
        transistor-level decks and UI-spaced cursors for RNM testbenches.
      </p>

      {open && (
        <div className="chan-body">
          <div className="chan-import">
            <label className="chan-file-label">
              Touchstone file
              <input
                ref={fileRef}
                type="file"
                accept=".s2p,.s4p"
                onChange={handleFile}
                disabled={busy}
              />
            </label>
            <div className="field-row chan-path-row">
              <label>
                ...or a path on this machine
                <input
                  type="text"
                  placeholder="/abs/path/to/channel.s4p"
                  value={pathValue}
                  onChange={(e) => setPathValue(e.target.value)}
                  disabled={busy}
                />
              </label>
              <button type="button" className="toggle-raw" onClick={handlePath} disabled={busy || !pathValue.trim()}>
                Import
              </button>
            </div>
          </div>

          {error && <div className="error-box">{error}</div>}

          {channels.length > 0 && (
            <div className="chan-list">
              {channels.map((c) => (
                <button
                  key={c.channel_id}
                  type="button"
                  className={`chan-list-item ${selectedId === c.channel_id ? "selected" : ""}`}
                  onClick={() => setSelectedId(c.channel_id)}
                >
                  <span className="chan-list-name">{c.name || c.filename}</span>
                  <span className="chan-list-meta">
                    {c.nports}-port
                    {c.state === "running" && " - fitting..."}
                    {c.status === "failed" && " - FAILED"}
                    {c.status === "success" && ` - h0 ${c.h0?.toFixed(3)}, max sv ${c.max_singular_value?.toFixed(3)}`}
                  </span>
                </button>
              ))}
            </div>
          )}

          {running && (
            <div className="chan-progress">
              <strong>Fitting {detail.filename}...</strong>
              <span className="hint">
                Searching pole count / fit bandwidth for a model that is passive at ALL
                frequencies. This takes tens of seconds to a few minutes; the result is
                cached, so the same file never refits.
              </span>
              <code>{detail.progress}</code>
            </div>
          )}

          {failed && (
            <div className="error-box">
              <strong>Channel import failed.</strong>
              <div>{detail.reason}</div>
              <div className="hint">
                No usable model was produced - nothing was emitted. The fit log below has
                every candidate that was tried and why it was rejected.
              </div>
            </div>
          )}

          {detail && detail.state === "done" && detail.status === "success" && (
            <>
              <Metrics channel={detail} />
              <p className="hint chan-contract">
                Artifacts: <code>{detail.dir}</code> - <code>channel.sp</code> (subckt{" "}
                <code>{detail.artifact_contract?.subckt_name}</code>, ports{" "}
                {detail.artifact_contract?.port_order?.join(" ")}) for SPICE decks,{" "}
                <code>cursors.txt</code> / <code>taps.hex</code> for RNM testbenches.
              </p>
            </>
          )}

          {detail && detail.state === "done" && <ArtifactViewer channelId={detail.channel_id} />}
        </div>
      )}
    </div>
  );
}
