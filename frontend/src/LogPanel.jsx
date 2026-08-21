import { useEffect, useRef, useState } from "react";
import { fetchLog } from "./api";

export default function LogPanel({ runId, running, kind = "run" }) {
  const [log, setLog] = useState("");
  const [error, setError] = useState(null);
  const preRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    setLog("");
    setError(null);
    if (!runId) return;

    let cancelled = false;
    async function poll() {
      try {
        const text = await fetchLog(runId, "session.log", kind);
        if (!cancelled) setLog(text);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }
    poll();
    if (running) {
      pollRef.current = setInterval(poll, 2000);
    }
    return () => {
      cancelled = true;
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [runId, running, kind]);

  // Auto-scroll to bottom as new log content arrives.
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [log]);

  return (
    <div className="log-panel">
      <div className="panel-header">
        <h3>Log output</h3>
        {running && <span className="live-dot" title="Live - updating every 2s" />}
      </div>
      {!runId && (
        <p className="hint">
          Submit a spec to see live {kind === "research" ? "Circuit_Researcher" : "Circuit_Builder"} output here.
        </p>
      )}
      {error && <div className="error-box small">{error}</div>}
      {runId && (
        <pre className="log-text" ref={preRef}>
          {log || "Waiting for output..."}
        </pre>
      )}
    </div>
  );
}
