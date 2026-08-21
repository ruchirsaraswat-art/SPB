import { useState } from "react";

/**
 * Shows a Circuit_Researcher topology-comparison run and lets the user pick
 * which candidate architecture to hand off to Circuit_Builder. This is the
 * "research/choose topology" step between the spec form and the build step:
 * spec form -> (this) -> build/simulate -> results.
 */
export default function ResearchView({ research, onProceed, onSkip, proceeding }) {
  const [selected, setSelected] = useState(null);

  if (!research) return null;

  if (research.state === "running") {
    return (
      <div className="results-view running">
        <h2>Researching topology options...</h2>
        <p>
          Circuit_Researcher is comparing candidate architectures for this
          block category against your spec (web search + its own knowledge
          base). This is usually faster than a full build, but can still
          take a couple of minutes - watch the log panel for live progress.
        </p>
        <div className="spinner" />
        <p className="hint">Use the "Stop" button at the top of the page to cancel this research step.</p>
      </div>
    );
  }

  const summary = research.summary || {};
  const failed = research.status !== "success";
  const candidates = summary.candidates || [];
  // Soft sky130-feasibility warnings persisted at submit time (same
  // mechanism as build runs - see ResultsView) so the comparison keeps its
  // "this spec was aggressive" context when viewed later.
  const warnings = research.warnings || [];
  const chosenName = selected ?? summary.recommended ?? (candidates[0] && candidates[0].name) ?? null;
  const chosenCandidate = candidates.find((c) => c.name === chosenName);

  return (
    <div className={`results-view ${failed ? "failed" : "success"}`}>
      <h2>{failed ? "Research failed" : "Topology recommendation"}</h2>

      {failed && (
        <div className="error-box">
          <strong>Reason:</strong> {research.reason || "Unknown failure"}
          {summary.error ? (
            <>
              <br />
              <strong>Circuit_Researcher reported:</strong> {summary.error}
            </>
          ) : null}
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

      {!failed && summary.category_label && (
        <p className="hint">
          Comparing architectures for: <strong>{summary.category_label}</strong>
        </p>
      )}

      {candidates.length > 0 && (
        <div className="candidate-list">
          {candidates.map((c) => (
            <label key={c.name} className={`candidate-card ${chosenName === c.name ? "chosen" : ""}`}>
              <div className="candidate-card-head">
                <input
                  type="radio"
                  name="candidate"
                  value={c.name}
                  checked={chosenName === c.name}
                  onChange={() => setSelected(c.name)}
                />
                <strong>{c.name}</strong>
                {summary.recommended === c.name && <span className="recommended-badge">Recommended</span>}
              </div>
              {c.summary && <p className="candidate-summary">{c.summary}</p>}
              <div className="candidate-pros-cons">
                {c.pros && c.pros.length > 0 && (
                  <div>
                    <span className="pc-label pc-pros">Pros</span>
                    <ul>
                      {c.pros.map((p, i) => (
                        <li key={i}>{p}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {c.cons && c.cons.length > 0 && (
                  <div>
                    <span className="pc-label pc-cons">Cons</span>
                    <ul>
                      {c.cons.map((cn, i) => (
                        <li key={i}>{cn}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </label>
          ))}
        </div>
      )}

      {summary.rationale && (
        <div className="notes-box">
          <strong>Circuit_Researcher's rationale for {summary.recommended}:</strong> {summary.rationale}
        </div>
      )}
      {summary.notes && (
        <div className="notes-box">
          <strong>Notes:</strong> {summary.notes}
        </div>
      )}

      <div className="research-actions">
        {candidates.length > 0 && (
          <button
            onClick={() => onProceed(chosenCandidate)}
            disabled={proceeding || !chosenCandidate}
          >
            {proceeding ? "Launching Circuit_Builder..." : `Build "${chosenName}"`}
          </button>
        )}
        <button className="toggle-raw" onClick={onSkip} disabled={proceeding}>
          {candidates.length > 0 ? "Skip research, build with default judgment" : "Continue without research"}
        </button>
      </div>
    </div>
  );
}
