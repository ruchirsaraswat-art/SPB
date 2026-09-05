import { useEffect, useState } from "react";
import { fetchWaveform } from "./api";

// Client-side SVG line plot (results sub-window, waveform half) - no
// server-side plotting dependency, per this feature's own scoping. Each
// selected trace is normalized to ITS OWN min/max (not a shared y-axis):
// the signals written to a raw file can be in different units (volts vs
// amps) and wildly different scales, so a shared axis would flatten the
// smaller ones to a flat line. The legend states each trace's real min/max
// so the normalization is never hiding the actual values.
const COLORS = ["#5fb3ff", "#ff8a5f", "#8dff5f", "#ff5fd6", "#ffd65f", "#5fffe0"];
const W = 640;
const H = 260;
const PAD_L = 46;
const PAD_R = 12;
const PAD_T = 10;
const PAD_B = 26;

function buildPath(xs, ys, xMin, xMax, yMin, yMax) {
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;
  let d = "";
  for (let i = 0; i < xs.length; i++) {
    if (xs[i] == null || ys[i] == null) continue;
    const px = PAD_L + ((xs[i] - xMin) / xSpan) * plotW;
    const py = PAD_T + plotH - ((ys[i] - yMin) / ySpan) * plotH;
    d += `${d ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)}`;
  }
  return d;
}

function SignalPlot({ sweep, sweepName, selected, data }) {
  const xMin = Math.min(...sweep.filter((v) => v != null));
  const xMax = Math.max(...sweep.filter((v) => v != null));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="waveform-plot-svg" role="img">
      <rect x={0} y={0} width={W} height={H} className="waveform-plot-bg" />
      <line x1={PAD_L} y1={H - PAD_B} x2={W - PAD_R} y2={H - PAD_B} className="waveform-axis" />
      <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={H - PAD_B} className="waveform-axis" />
      <text x={PAD_L} y={H - 6} className="waveform-axis-label">
        {sweepName} — {xMin.toPrecision(4)} .. {xMax.toPrecision(4)}
      </text>
      {selected.map((name, i) => {
        const ys = data[name] || [];
        const finite = ys.filter((v) => v != null);
        if (finite.length === 0) return null;
        const yMin = Math.min(...finite);
        const yMax = Math.max(...finite);
        const path = buildPath(sweep, ys, xMin, xMax, yMin, yMax);
        return <path key={name} d={path} className="waveform-trace" stroke={COLORS[i % COLORS.length]} />;
      })}
    </svg>
  );
}

export default function WaveformViewer({ waveform, waveformUrlBase }) {
  const files = waveform?.files || [];
  const [fileName, setFileName] = useState(files[0]?.name || "");
  const [data, setData] = useState(null); // parsed backend response
  const [selected, setSelected] = useState([]); // signal names currently plotted
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!fileName) return undefined;
    let stale = false;
    setLoading(true);
    setError(null);
    setData(null);
    fetchWaveform(waveformUrlBase, fileName)
      .then((d) => {
        if (stale) return;
        setData(d);
        // Default view: every non-sweep signal, capped at 4 so a testbench
        // that wrote more than a handful of nodes doesn't render an
        // unreadable overlay by default - the checkboxes below can add more.
        const others = (d.signals || []).filter((s) => s !== d.sweep_var);
        setSelected(others.slice(0, 4));
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
  }, [fileName, waveformUrlBase]);

  if (!waveform) return null;
  if (!waveform.available) {
    return (
      <div className="waveform-viewer">
        <h3>Waveform</h3>
        <p className="hint waveform-empty">{waveform.reason}</p>
      </div>
    );
  }

  function toggle(name) {
    setSelected((sel) => (sel.includes(name) ? sel.filter((s) => s !== name) : [...sel, name]));
  }

  return (
    <div className="waveform-viewer">
      <h3>Waveform</h3>
      {files.length > 1 && (
        <label className="waveform-file-picker">
          File
          <select value={fileName} onChange={(e) => setFileName(e.target.value)}>
            {files.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name} ({(f.size_bytes / 1024).toFixed(0)} KB)
              </option>
            ))}
          </select>
        </label>
      )}
      {loading && <p className="hint">Parsing {fileName}...</p>}
      {error && <div className="error-box small">{error}</div>}
      {!loading && !error && data && (
        <>
          <div className="waveform-signal-picker">
            {(data.signals || [])
              .filter((s) => s !== data.sweep_var)
              .map((s) => (
                <label key={s} className="waveform-signal-checkbox">
                  <input type="checkbox" checked={selected.includes(s)} onChange={() => toggle(s)} />
                  {s}
                </label>
              ))}
          </div>
          {selected.length > 0 ? (
            <SignalPlot sweep={data.data[data.sweep_var] || []} sweepName={data.sweep_var} selected={selected} data={data.data} />
          ) : (
            <p className="hint">Pick at least one signal above to plot it.</p>
          )}
          <ul className="waveform-legend">
            {selected.map((name, i) => {
              const finite = (data.data[name] || []).filter((v) => v != null);
              const yMin = finite.length ? Math.min(...finite) : null;
              const yMax = finite.length ? Math.max(...finite) : null;
              return (
                <li key={name}>
                  <span className="waveform-legend-swatch" style={{ background: COLORS[i % COLORS.length] }} />
                  {name}
                  {yMin != null ? ` — ${yMin.toPrecision(4)} .. ${yMax.toPrecision(4)}` : ""}
                </li>
              );
            })}
          </ul>
          <p className="hint">
            {data.points.toLocaleString()} point{data.points === 1 ? "" : "s"} total
            {data.downsampled ? ` (downsampled to ${data.returned_points.toLocaleString()} for display)` : ""}.
          </p>
        </>
      )}
    </div>
  );
}
