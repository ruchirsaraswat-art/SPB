import { useCallback, useEffect, useRef, useState } from "react";
import SpecForm from "./SpecForm";
import ResearchView from "./ResearchView";
import ResultsView from "./ResultsView";
import LogPanel from "./LogPanel";
import SchematicPanel from "./SchematicPanel";
import SettingsPanel from "./SettingsPanel";
import { createRun, getRun, createResearch, getResearch, cancelRun, cancelResearch, listRuns, getSettings } from "./api";
import "./App.css";

// Message to show in the error box. api.js throws Error objects whose
// .message is already formatted for display (including multi-line 422
// validation breakdowns - .error-box is white-space: pre-wrap); String(e)
// would prepend a noisy "Error: " to that.
function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

export default function App() {
  // Flow: spec form -> Circuit_Researcher compares candidate architectures
  // for whatever topology was picked (including the built-in NMOS current
  // mirror - a "simple two-transistor" mirror is itself just one candidate
  // among cascode/Wilson/etc.) -> user picks one (or skips) -> build/simulate
  // -> results.
  const [pendingSpec, setPendingSpec] = useState(null); // spec as submitted from the form, kept around until build fires
  const [research, setResearch] = useState(null); // Circuit_Researcher run, or null if skipped/not started
  const [researching, setResearching] = useState(false);
  const [proceeding, setProceeding] = useState(false); // building right after a research pick

  const [run, setRun] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const runPollRef = useRef(null);
  const researchPollRef = useRef(null);

  // Complete-architecture view state: which PHY/block/topology the form is
  // on (topology + label feed the schematic sub-window), plus run history so
  // already-built blocks stay marked as the arch matures.
  const [archView, setArchView] = useState({
    phy_type: "ser-des",
    selected_block: null,
    topology: null,
    topology_label: null,
  });
  const [allRuns, setAllRuns] = useState([]);

  const handleArchChange = useCallback((a) => {
    setArchView((prev) =>
      prev.phy_type === a.phy_type &&
      prev.selected_block === a.selected_block &&
      prev.topology === a.topology &&
      prev.topology_label === a.topology_label
        ? prev
        : a
    );
  }, []);

  const refreshRuns = useCallback(() => {
    listRuns().then(setAllRuns).catch(() => {});
  }, []);

  useEffect(() => {
    refreshRuns();
  }, [refreshRuns]);

  // Tool settings (cycle 5): where the tool creates runs/libraries. Fetched
  // once here and passed down so the spec form can show where things will
  // land; the settings panel updates it in place on save.
  const [settings, setSettings] = useState(null);
  const [settingsError, setSettingsError] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  useEffect(() => {
    getSettings()
      .then((s) => {
        setSettings(s);
        setSettingsError(null);
      })
      .catch((e) => setSettingsError(errText(e)));
  }, []);

  // Stopping is handled centrally by one always-visible top bar (see
  // <ActiveRunBar> below) rather than a button buried inside whichever
  // panel happens to be showing - that was easy to miss (e.g. before the
  // run/research object has synced from its first poll, or if you're
  // scrolled past it), so there's exactly one obvious place to stop
  // anything that's in flight.
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState(null);

  useEffect(() => {
    // Allow deep-linking to a past run for debugging/review: ?run=<run_id>
    const params = new URLSearchParams(window.location.search);
    const existingRunId = params.get("run");
    if (existingRunId) {
      getRun(existingRunId)
        .then(setRun)
        .catch((e) => setError(errText(e)));
    }
    return () => {
      if (runPollRef.current) clearInterval(runPollRef.current);
      if (researchPollRef.current) clearInterval(researchPollRef.current);
    };
  }, []);

  function startRunPolling(runId) {
    runPollRef.current = setInterval(async () => {
      try {
        const updated = await getRun(runId);
        setRun(updated);
        if (updated.state === "done") {
          clearInterval(runPollRef.current);
          setSubmitting(false);
          setProceeding(false);
          refreshRuns(); // a finished build changes block maturity in the architecture diagram
        }
      } catch (e) {
        clearInterval(runPollRef.current);
        setSubmitting(false);
        setProceeding(false);
        setError(errText(e));
      }
    }, 3000);
  }

  function startResearchPolling(researchId) {
    researchPollRef.current = setInterval(async () => {
      try {
        const updated = await getResearch(researchId);
        setResearch(updated);
        if (updated.state === "done") {
          clearInterval(researchPollRef.current);
          setResearching(false);
        }
      } catch (e) {
        clearInterval(researchPollRef.current);
        setResearching(false);
        setError(errText(e));
      }
    }, 2000);
  }

  async function launchBuild(spec) {
    setError(null);
    setSubmitting(true);
    setRun(null);
    try {
      const { run_id } = await createRun(spec);
      const initial = await getRun(run_id);
      setRun(initial);
      startRunPolling(run_id);
    } catch (e) {
      setSubmitting(false);
      setProceeding(false);
      setError(errText(e));
    }
  }

  async function handleSubmit(spec) {
    setError(null);
    setRun(null);
    setResearch(null);
    setPendingSpec(spec);

    setResearching(true);
    try {
      const researchSpec = {
        label: spec.label,
        topology: spec.topology,
        custom_topology: spec.custom_topology,
        extra_fields: spec.extra_fields,
        power_mw: spec.power_mw,
        area_um2: spec.area_um2,
        notes: spec.notes,
        vdd_v: spec.vdd_v,
        temp_c: spec.temp_c,
        corner: spec.corner,
        phy_type: spec.phy_type,
        selected_block: spec.selected_block,
      };
      const { research_id } = await createResearch(researchSpec);
      const initial = await getResearch(research_id);
      setResearch(initial);
      startResearchPolling(research_id);
    } catch (e) {
      setResearching(false);
      setError(errText(e));
    }
  }

  async function handleProceedWithCandidate(candidate) {
    if (!pendingSpec || !candidate) return;
    setProceeding(true);
    await launchBuild({
      ...pendingSpec,
      chosen_architecture: candidate.name,
      chosen_architecture_rationale: candidate.summary || null,
    });
  }

  async function handleSkipResearch() {
    if (!pendingSpec) return;
    setProceeding(true);
    await launchBuild(pendingSpec);
  }

  function handleRunCancelled() {
    if (run) {
      getRun(run.run_id).then(setRun).catch(() => {});
    }
  }

  function handleResearchCancelled() {
    if (research) {
      getResearch(research.research_id).then(setResearch).catch(() => {});
    }
  }

  // Once a build has actually started, that's the primary thing to show
  // (research becomes a recap above it, not a competing panel).
  const showBuild = !!run;
  const showResearch = !showBuild && !!research;

  // What's actually cancellable right now, if anything. `submitting`/
  // `researching` go true the instant the user clicks the form's submit
  // button (before the create-request has even round-tripped), so the bar
  // appears immediately - it just can't act until a run_id/research_id
  // exists to send a cancel request for.
  const activeRun = showBuild && run?.state === "running";
  const activeResearch = showResearch && research?.state === "running";
  const isActive = activeRun || activeResearch || submitting || researching;
  const canStopNow = (activeRun && run?.run_id) || (activeResearch && research?.research_id);

  // Per-block maturity for the complete-architecture diagram, keyed directly
  // by each run's selected_block id (so it works for default, user-added,
  // and hierarchy-nested blocks alike). Precedence: what's happening right
  // now (exploring/building) > run history (built/failed) > currently
  // selected in the form > untouched.
  const blockStates = {};
  for (const r of allRuns) {
    if (r.phy_type !== archView.phy_type || !r.selected_block || r.state !== "done") continue;
    const cur = blockStates[r.selected_block];
    if (r.status === "success") {
      if (cur?.status !== "built") {
        blockStates[r.selected_block] = { status: "built", detail: r.chosen_architecture || r.topology };
      }
    } else if (!cur) {
      blockStates[r.selected_block] = { status: "failed", detail: r.chosen_architecture || r.topology };
    }
  }
  if (archView.selected_block && !blockStates[archView.selected_block]) {
    blockStates[archView.selected_block] = { status: "selected" };
  }
  const liveSpec = run?.spec || pendingSpec;
  if (liveSpec?.phy_type === archView.phy_type && liveSpec?.selected_block) {
    const id = liveSpec.selected_block;
    if (activeRun || submitting || proceeding) {
      blockStates[id] = { status: "building", detail: liveSpec.chosen_architecture };
    } else if (researching || activeResearch || showResearch) {
      blockStates[id] = { status: "exploring" };
    } else if (run?.state === "done" && run?.status === "success") {
      blockStates[id] = { status: "built", detail: run.spec?.chosen_architecture || run.spec?.topology };
    }
  }

  async function handleStopActive() {
    setStopping(true);
    setStopError(null);
    try {
      if (activeRun) {
        await cancelRun(run.run_id);
        handleRunCancelled();
      } else if (activeResearch) {
        await cancelResearch(research.research_id);
        handleResearchCancelled();
      }
    } catch (e) {
      setStopError(errText(e));
    } finally {
      setStopping(false);
    }
  }

  return (
    <div className="app-shell">
      <header>
        <div className="header-row">
          <h1>SPB-Saraswat PHY Builder</h1>
          <button
            type="button"
            className="settings-gear"
            onClick={() => setSettingsOpen((o) => !o)}
            title="Tool settings (working directory)"
            aria-label="Tool settings"
          >
            ⚙ Settings
          </button>
        </div>
        <p className="subtitle">
          Spec-driven circuit generator for sky130, via Circuit_Researcher (topology comparison) + Circuit_Builder
          (netlist/schematic/simulation)
        </p>
      </header>

      {settingsOpen && (
        <div className="panel">
          <SettingsPanel
            settings={settings}
            error={settingsError}
            onSaved={setSettings}
            onClose={() => setSettingsOpen(false)}
          />
        </div>
      )}

      {isActive && (
        <div className="active-run-bar">
          <span className="live-dot" />
          <span>
            {activeResearch
              ? "Circuit_Researcher is running..."
              : activeRun
              ? "Circuit_Builder is running..."
              : "Starting..."}
          </span>
          <button className="stop-btn" onClick={handleStopActive} disabled={stopping || !canStopNow}>
            {stopping ? "Stopping..." : "Stop"}
          </button>
          {stopError && <span className="error-inline">{stopError}</span>}
        </div>
      )}

      <main>
        {/* Schematic sub-window (cycle 4): rides alongside the spec form and
            follows whatever block/topology is currently highlighted; nothing
            selected -> full-width form, no dangling empty panel. */}
        <div className={archView.topology ? "split-panels form-with-schematic" : undefined}>
          <div className="panel">
            <SpecForm
              onSubmit={handleSubmit}
              submitting={researching || submitting}
              onArchChange={handleArchChange}
              blockStates={blockStates}
              settings={settings}
            />
          </div>
          {archView.topology && (
            <div className="panel schematic-panel-slot">
              <SchematicPanel
                topology={archView.topology}
                topologyLabel={archView.topology_label}
                blockId={archView.selected_block}
              />
            </div>
          )}
        </div>

        {error && <div className="error-box">{error}</div>}

        {showBuild && research && (
          <div className="research-recap">
            <strong>Topology chosen from research:</strong>{" "}
            {run.spec?.chosen_architecture || "default judgment (research skipped)"}
            {research.summary?.category_label ? ` — category: ${research.summary.category_label}` : ""}
          </div>
        )}

        {showResearch && (
          <div className="split-panels">
            <div className="panel results-panel">
              <ResearchView
                research={research}
                onProceed={handleProceedWithCandidate}
                onSkip={handleSkipResearch}
                proceeding={proceeding}
              />
            </div>
            <div className="panel">
              <LogPanel runId={research.research_id} running={research.state === "running"} kind="research" />
            </div>
          </div>
        )}

        {showBuild && (
          <div className="split-panels">
            <div className="panel results-panel">
              <ResultsView run={run} />
            </div>
            <div className="panel">
              <LogPanel runId={run.run_id} running={run.state === "running"} kind="run" />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
