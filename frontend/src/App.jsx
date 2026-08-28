import { useCallback, useEffect, useRef, useState } from "react";
import SpecForm from "./SpecForm";
import ResearchView from "./ResearchView";
import ResultsView from "./ResultsView";
import LogPanel from "./LogPanel";
import SchematicPanel from "./SchematicPanel";
import SettingsPanel from "./SettingsPanel";
import ArchChatSidebar from "./ArchChatSidebar";
import InterfaceEditor from "./InterfaceEditor";
import DigitalArchPanel from "./DigitalArchPanel";
import FirmwarePanel from "./FirmwarePanel";
import PhyOverviewPanel from "./PhyOverviewPanel";
import PhyTypeSelect from "./PhyTypeSelect";
import { createRun, getRun, createResearch, getResearch, cancelRun, cancelResearch, listRuns, getSettings, listArchitectures, rtlGenerate } from "./api";
import { loadArchitectureIntoPhy } from "./phyArchitectures";
import "./App.css";

// The workbench workspaces (2026-08-23 digital-arch spec, section d; layout
// + naming revised per user request the same day): below the top-level "PHY
// architecture" overview, the AFE diagram+spec form, the interface table
// editor and the controller (digital) architecture are stacked ONE BELOW
// THE OTHER at full width - mirroring the physical signal path
// AFE -> interface -> controller - each individually minimizable to its
// header bar. One panel is "focused" at a time (click its header) and the
// chat sidebar context-switches with it (arch_chat view afe / if_chat /
// arch_chat view digital). "Controller" is a DISPLAY name only: internal
// ids, session ids, localStorage namespaces and the API view value stay
// "digital"/"interface".
// "Firmware" (2026-08-23 firmware-section spec) is the fifth stacked panel,
// at the bottom to match the control-plane chain order
// (AFE -> interface -> controller -> firmware).
const WORKSPACES = [
  { id: "afe", title: "PHY / AFE architecture" },
  { id: "interface", title: "AFE ↔ Controller interface" },
  { id: "digital", title: "PHY / Controller architecture" },
  { id: "firmware", title: "PHY / Firmware" },
];

// Focus mode (user scope decision, 2026-08-27): "let us focus on AFE only
// and only on DDR design to start with". PRESENTATION-LEVEL ONLY - no code
// or feature is removed: while the toggle (Settings panel, "Focus: DDR AFE
// only") is on, the interface/controller/firmware workspaces are simply not
// rendered, the PHY type pull-down pins to DDR (others disabled, "coming
// later"), the overview greys out the non-AFE blocks, and the AFE panel
// starts expanded. Toggling it off restores everything. Default ON;
// persisted in localStorage like the other per-UI state in this app.
const FOCUS_KEY = "focus-ddr-afe";
function loadFocusMode() {
  try {
    const stored = localStorage.getItem(FOCUS_KEY);
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}

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
  const [archView, setArchView] = useState(() => ({
    phy_type: loadFocusMode() ? "ddr" : "ser-des",
    selected_block: null,
    topology: null,
    topology_label: null,
  }));
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

  // Arch-chat feature: the displayed architecture is mutable (chat patches,
  // saved-architecture loads) - archVersion bumps whenever it was edited
  // outside the diagram component so the diagram re-reads its records, and
  // customArchs feeds the "Custom (saved architectures)" optgroup in the
  // PHY selector (built-ins stay static in phyArchitectures.js).
  const [archVersion, setArchVersion] = useState(0);
  // Same version-bump pattern for the other two workspaces: the digital
  // diagram's "dig-arch-" records and the interface working copy are both
  // mutated outside their panels (chat patch apply/undo in the sidebar), so
  // a bump tells them to re-read.
  const [digVersion, setDigVersion] = useState(0);
  const [ifVersion, setIfVersion] = useState(0);
  const [fwVersion, setFwVersion] = useState(0);
  const bumpInterface = useCallback(() => setIfVersion((v) => v + 1), []);
  // The sidebar reports which diagram workspace it edited
  // ("afe"/"digital"/"firmware").
  const handleArchEdited = useCallback((workspace) => {
    if (workspace === "digital") setDigVersion((v) => v + 1);
    else if (workspace === "firmware") setFwVersion((v) => v + 1);
    else setArchVersion((v) => v + 1);
  }, []);
  // Which workspace panel is focused; the chat sidebar follows it.
  const [focusedWorkspace, setFocusedWorkspace] = useState("afe");
  // Per-panel minimize (collapse to the header bar) - independent of focus.
  // "overview" is the top-level PHY-architecture depiction (navigational
  // only, not a chat workspace). Default on load (user request, 2026-08-23):
  // ONLY the overview starts expanded - the workspace panels all start
  // minimized and are opened via the overview blocks (which un-minimize +
  // scroll) or their own header buttons. Minimize state is deliberately not
  // persisted, so this collapsed default applies on every load.
  // Focus-mode exception (2026-08-27): with "Focus: DDR AFE only" on, the
  // AFE panel is the whole point of the page, so it starts EXPANDED.
  const [minimized, setMinimized] = useState(() => ({
    overview: false,
    afe: !loadFocusMode(),
    interface: true,
    digital: true,
    firmware: true,
  }));
  const toggleMinimized = useCallback((id) => {
    setMinimized((m) => ({ ...m, [id]: !m[id] }));
  }, []);
  // Overview-block navigation (UX fix, 2026-08-23): with the stacked layout
  // the target workspace can be off-screen, so clicking an overview block
  // must do more than set focus - it un-minimizes the target panel and
  // scrolls it into view. The scroll happens in an effect (after React has
  // committed the un-minimize) via a ref on each workspace <section>;
  // `n` makes repeat clicks on the same block re-trigger the effect.
  // The panels carry scroll-margin-top in CSS so the title lands clear of
  // any sticky bar.
  const workspaceRefs = useRef({});
  const [scrollTarget, setScrollTarget] = useState(null); // { id, n }
  useEffect(() => {
    if (!scrollTarget) return;
    workspaceRefs.current[scrollTarget.id]?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [scrollTarget]);
  const handleOverviewFocus = useCallback((id) => {
    setFocusedWorkspace(id); // keep the chat sidebar following
    setMinimized((m) => (m[id] ? { ...m, [id]: false } : m));
    setScrollTarget({ id, n: Date.now() });
  }, []);
  const [customArchs, setCustomArchs] = useState([]);
  const refreshCustomArchs = useCallback(() => {
    listArchitectures()
      .then((d) => setCustomArchs(d.architectures || []))
      .catch(() => {});
  }, []);
  useEffect(() => {
    refreshCustomArchs();
  }, [refreshCustomArchs]);

  const handleLoadCustomArch = useCallback((entry, phy) => {
    loadArchitectureIntoPhy(phy, entry.architecture);
    setArchVersion((v) => v + 1);
  }, []);

  // The PHY Type / Architecture pull-down (moved out of SpecForm into the
  // overview panel's header, user request 2026-08-23): App owns the choice
  // because switching PHY type re-targets all three workspaces. archChoice
  // is what the <select> shows (built-in value or "custom:<name>"); phyType
  // is the resulting active PHY, pushed down into SpecForm as a controlled
  // prop (which clears its selected block on change - same contract as when
  // the select lived inside the form).
  const [archChoice, setArchChoice] = useState(() => (loadFocusMode() ? "ddr" : "ser-des"));
  const [phyType, setPhyType] = useState(() => (loadFocusMode() ? "ddr" : "ser-des"));
  const handlePhyChoice = useCallback(
    (v) => {
      setArchChoice(v);
      if (v.startsWith("custom:")) {
        const entry = customArchs.find((a) => a.name === v.slice("custom:".length));
        if (entry) {
          const phy = entry.phy_type || phyType;
          handleLoadCustomArch(entry, phy);
          setPhyType(phy);
        }
      } else {
        setPhyType(v);
      }
    },
    [customArchs, phyType, handleLoadCustomArch]
  );

  // Focus mode toggle (see FOCUS_KEY above). Turning it ON pins the PHY to
  // DDR, expands the AFE panel and pulls focus back to it (the hidden
  // workspaces can't stay focused - the chat sidebar follows focus).
  // Turning it OFF just re-renders the hidden panels; their minimize state
  // and the DDR selection are left as they are.
  const [focusDdrAfe, setFocusDdrAfe] = useState(loadFocusMode);
  const handleFocusChange = useCallback((on) => {
    setFocusDdrAfe(on);
    try {
      localStorage.setItem(FOCUS_KEY, on ? "1" : "0");
    } catch {
      // localStorage unavailable - the toggle just won't persist
    }
    if (on) {
      setArchChoice("ddr");
      setPhyType("ddr");
      setMinimized((m) => (m.afe ? { ...m, afe: false } : m));
      setFocusedWorkspace("afe");
    }
  }, []);
  const visibleWorkspaces = focusDdrAfe ? WORKSPACES.filter((w) => w.id === "afe") : WORKSPACES;

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

    // Symbol-only, model-only and architecture-model deliverables skip the
    // Circuit_Researcher step entirely: there is no single architecture to
    // choose (no transistor-level design happens).
    if (["symbol", "veriloga", "verilog", "rnm", "arch_model", "arch_stitch"].includes(spec.deliverable)) {
      await launchBuild(spec);
      return;
    }

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

  // RTL generation (2026-08-23 rtl-gen spec): the controller panel submits a
  // full rtl_generate request; the run then polls/display exactly like every
  // other run (deliverable "rtl" - ResultsView renders the RTL section).
  async function handleRtlSubmit(request) {
    setError(null);
    setResearch(null);
    setPendingSpec(null);
    setRun(null);
    setSubmitting(true);
    try {
      const { run_id } = await rtlGenerate(request);
      const initial = await getRun(run_id);
      setRun(initial);
      startRunPolling(run_id);
    } catch (e) {
      setSubmitting(false);
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
            focusDdrAfe={focusDdrAfe}
            onFocusChange={handleFocusChange}
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
              ? run?.deliverable === "rtl"
                ? "RTL_Coder is running..."
                : "Circuit_Builder is running..."
              : "Starting..."}
          </span>
          <button className="stop-btn" onClick={handleStopActive} disabled={stopping || !canStopNow}>
            {stopping ? "Stopping..." : "Stop"}
          </button>
          {stopError && <span className="error-inline">{stopError}</span>}
        </div>
      )}

      <main>
        {/* Workbench (2026-08-23, layout per user request): the three
            workspace panels - PHY/AFE architecture, digital architecture,
            AFE<->digital interface - stacked one below the other at full
            width. Each is individually minimizable to its header bar; one
            is "focused" (click a header) and the chat sidebar on the right
            context-switches with it. */}
        <div className="drawing-with-chat">
          <div className="drawing-main workbench">
            {/* Top-level PHY architecture: Controller + AFE blocks joined by
                the interface - static + navigational (clicking a block
                focuses its workspace). Not a chat workspace of its own. */}
            <section
              className={`panel workspace-panel overview-panel ${minimized.overview ? "minimized" : ""}`}
              data-workspace="overview"
            >
              <header className="workspace-head overview-head">
                <span className="overview-head-left">
                  <h2>PHY architecture</h2>
                  {/* Selector kept in the HEADER (not the body) so it stays
                      reachable while the overview panel is minimized. */}
                  <PhyTypeSelect
                    archChoice={archChoice}
                    customArchs={customArchs}
                    onChange={handlePhyChoice}
                    focusDdrOnly={focusDdrAfe}
                  />
                </span>
                <span className="workspace-head-right">
                  <button
                    type="button"
                    className="toggle-raw workspace-min-btn"
                    onClick={() => toggleMinimized("overview")}
                    title={minimized.overview ? "Expand the PHY architecture overview" : "Minimize the PHY architecture overview to its header"}
                  >
                    {minimized.overview ? "Expand" : "Minimize"}
                  </button>
                </span>
              </header>
              {!minimized.overview && (
                <div className="workspace-body">
                  <PhyOverviewPanel
                    phyType={archView.phy_type}
                    focusedWorkspace={focusedWorkspace}
                    onFocus={handleOverviewFocus}
                    focusDdrAfe={focusDdrAfe}
                  />
                </div>
              )}
            </section>
            {visibleWorkspaces.map(({ id, title }) => (
              <section
                key={id}
                ref={(el) => (workspaceRefs.current[id] = el)}
                className={`panel workspace-panel ${focusedWorkspace === id ? "focused" : "unfocused"} ${minimized[id] ? "minimized" : ""}`}
                data-workspace={id}
              >
                <header
                  className="workspace-head"
                  onClick={() => setFocusedWorkspace(id)}
                  title={focusedWorkspace === id ? undefined : `Focus the ${title} workspace (the chat follows it)`}
                >
                  <h2>{title}</h2>
                  <span className="workspace-head-right">
                    {focusedWorkspace === id ? (
                      <span className="workspace-focus-hint focused-hint">chat follows this panel</span>
                    ) : (
                      <span className="workspace-focus-hint">click to focus</span>
                    )}
                    <button
                      type="button"
                      className="toggle-raw workspace-min-btn"
                      onClick={(e) => {
                        e.stopPropagation(); // minimize without stealing focus
                        toggleMinimized(id);
                      }}
                      title={minimized[id] ? `Expand the ${title} panel` : `Minimize the ${title} panel to its header`}
                    >
                      {minimized[id] ? "Expand" : "Minimize"}
                    </button>
                  </span>
                </header>
                {!minimized[id] && (
                  <div className="workspace-body">
                    {id === "afe" && (
                      <SpecForm
                        onSubmit={handleSubmit}
                        submitting={researching || submitting}
                        onArchChange={handleArchChange}
                        blockStates={blockStates}
                        settings={settings}
                        archVersion={archVersion}
                        phyType={phyType}
                      />
                    )}
                    {id === "interface" && (
                      <InterfaceEditor phyType={archView.phy_type} version={ifVersion} />
                    )}
                    {id === "digital" && (
                      <DigitalArchPanel
                        phyType={archView.phy_type}
                        version={digVersion}
                        onRtlSubmit={handleRtlSubmit}
                        rtlSubmitting={submitting}
                      />
                    )}
                    {id === "firmware" && (
                      <FirmwarePanel phyType={archView.phy_type} version={fwVersion} />
                    )}
                  </div>
                )}
              </section>
            ))}
          </div>
          {/* Right column: the context-switching consultant chat on top, and
              (user request) the schematic sub-window kept UNDERNEATH it - it
              still follows whatever block/topology is highlighted in the
              form. */}
          <div className="drawing-side">
            <ArchChatSidebar
              phyType={archView.phy_type}
              workspace={focusedWorkspace}
              onArchEdited={handleArchEdited}
              onInterfaceEdited={bumpInterface}
              onSaved={refreshCustomArchs}
            />
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
