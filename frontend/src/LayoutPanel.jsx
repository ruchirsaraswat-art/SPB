import { useEffect, useRef, useState } from "react";
import {
  apiUrl,
  createLibrary,
  createBlankLayout,
  captureLayout,
  layoutPrereqs,
  layoutResolutions,
  layoutWsUrl,
  LayoutExistsError,
  listLibraries,
  resolveLayout,
  resolveSchematic,
  restartLayoutSession,
  startLayoutCellSession,
  stopLayoutSession,
} from "./api";

const NEW_LIBRARY = "__new__"; // same sentinel SpecForm's/SchematicPanel's library picker uses

// Same charset SchematicPanel.jsx's sanitizeCellName enforces (letters,
// digits, '_'/'-', starting with a letter or '_', max 64 chars).
function sanitizeCellName(raw) {
  let s = (raw || "").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
  if (!/^[a-z_]/.test(s)) s = `_${s}`;
  return s.slice(0, 64) || "new_cell";
}

// Same two independent resize levers as SchematicPanel.jsx's live xschem
// session (view size = CSS-only/instant; display resolution = destructive
// restart) - see that file's comment for the full rationale, unchanged here.
const VIEW_SIZE_KEY = "layout-live-view-size";
const START_RESOLUTION_KEY = "layout-live-start-resolution";
const VIEW_SIZES = ["small", "medium", "large", "fit"];
const VIEW_SIZE_LABELS = { small: "Small", medium: "Medium", large: "Large", fit: "Fit width" };

function loadViewSize() {
  try {
    const v = localStorage.getItem(VIEW_SIZE_KEY);
    return VIEW_SIZES.includes(v) ? v : "fit";
  } catch {
    return "fit";
  }
}

function saveViewSize(size) {
  try {
    localStorage.setItem(VIEW_SIZE_KEY, size);
  } catch {
    /* not persisted this session */
  }
}

function loadStartResolution(fallback) {
  try {
    return localStorage.getItem(START_RESOLUTION_KEY) || fallback;
  } catch {
    return fallback;
  }
}

function saveStartResolution(resolution) {
  try {
    localStorage.setItem(START_RESOLUTION_KEY, resolution);
  } catch {
    /* not persisted this session */
  }
}

// Layout sub-window (2026-09): the magic counterpart to SchematicPanel - same
// block-selection-driven docking, same live-in-browser-VNC mechanics (shares
// backend/xschem_session.py's session engine - see backend/layout.py), same
// create-if-missing empty state. Deliberately does NOT show a static
// rendered preview or offer a "capture" action: batch magic (any headless
// render/DRC/extraction) is unreliable on this install (see backend/
// layout.py's module docstring) - the live session IS the view.
export default function LayoutPanel({ topology, topologyLabel, blockId }) {
  const [schResolved, setSchResolved] = useState(null); // GET /api/schematic/resolve - just for its library/cell, if any
  const [resolved, setResolved] = useState(null); // GET /api/layout/resolve/{library}/{cell}
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(true); // starts collapsed - schematic panel is the primary view

  const [prereqs, setPrereqs] = useState(null);
  const [liveSession, setLiveSession] = useState(null);
  const [liveStarting, setLiveStarting] = useState(false);
  const [liveStatus, setLiveStatus] = useState(null);
  const rfbRef = useRef(null);
  const canvasHostRef = useRef(null);

  const [resolutions, setResolutions] = useState(null);
  const [startResolution, setStartResolution] = useState("");
  const [viewSize, setViewSize] = useState(loadViewSize);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [resizeChoice, setResizeChoice] = useState("");
  const [resizeBusy, setResizeBusy] = useState(false);
  const [resizeMsg, setResizeMsg] = useState(null);

  // --- create-a-new-layout state (empty-state action) ------------------------
  const [libraries, setLibraries] = useState([]);
  const [libChoice, setLibChoice] = useState("");
  const [newLibName, setNewLibName] = useState("");
  const [libBusy, setLibBusy] = useState(false);
  const [libError, setLibError] = useState(null);
  const [cellName, setCellName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createMsg, setCreateMsg] = useState(null);
  const [capturing, setCapturing] = useState(false);
  const [captureMsg, setCaptureMsg] = useState(null);

  useEffect(() => {
    let stale = false;
    listLibraries()
      .then((data) => {
        if (!stale) setLibraries(data.libraries || []);
      })
      .catch(() => {
        if (!stale) setLibraries([]);
      });
    return () => {
      stale = true;
    };
  }, []);

  useEffect(() => {
    let stale = false;
    layoutPrereqs()
      .then((p) => {
        if (!stale) setPrereqs(p);
      })
      .catch((e) => {
        if (!stale) {
          setPrereqs({
            available: false,
            message: e instanceof Error ? e.message : "Could not check layout (magic) prerequisites.",
          });
        }
      });
    return () => {
      stale = true;
    };
  }, []);

  useEffect(() => {
    let stale = false;
    layoutResolutions()
      .then((r) => {
        if (stale) return;
        setResolutions(r);
        setStartResolution((cur) => cur || loadStartResolution(r.default));
      })
      .catch(() => {
        /* non-fatal - start_session()'s own DEFAULT_RESOLUTION applies */
      });
    return () => {
      stale = true;
    };
  }, []);

  useEffect(() => {
    function onFsChange() {
      setIsFullscreen(document.fullscreenElement === canvasHostRef.current);
    }
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  useEffect(() => {
    setResizeChoice(liveSession?.resolution || "");
    setResizeMsg(null);
  }, [liveSession?.session_id, liveSession?.resolution]);

  // Resolve: which library/cell (if any) is this block already filed at
  // (from the schematic side), then whether THAT cell has a layout yet.
  useEffect(() => {
    if (!topology) {
      setSchResolved(null);
      setResolved(null);
      return undefined;
    }
    let stale = false;
    setLoading(true);
    setError(null);
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
    setLiveSession(null);
    setLiveStatus(null);
    setLibChoice("");
    setNewLibName("");
    setLibError(null);
    setCellName(sanitizeCellName(blockId || topology));
    setCreateMsg(null);

    resolveSchematic(topology)
      .then((sch) => {
        if (stale) return;
        setSchResolved(sch);
        if (sch.found && sch.source === "library") {
          setLibChoice(sch.library);
          setCellName(sch.cell);
          return resolveLayout(sch.library, sch.cell);
        }
        return { found: false, message: "File this block as a library cell first (via the Schematic "
          + "panel above), or create a layout-only cell below." };
      })
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

  useEffect(() => {
    return () => {
      if (rfbRef.current) {
        rfbRef.current.disconnect();
        rfbRef.current = null;
      }
    };
  }, []);

  // Mount the noVNC client - identical approach to SchematicPanel.jsx (see
  // that file's comment for why the vendored core/rfb.js is loaded as a
  // runtime module URL rather than bundled, and why scaleViewport stays
  // false).
  useEffect(() => {
    if (!liveSession || !canvasHostRef.current) return undefined;
    let cancelled = false;
    let createdRfb = null;
    const novncRfbUrl = ["", "novnc", "core", "rfb.js"].join("/");
    import(/* @vite-ignore */ novncRfbUrl).then((mod) => {
      if (cancelled || !canvasHostRef.current) return;
      const RFB = mod.default;
      const rfb = new RFB(canvasHostRef.current, layoutWsUrl(liveSession.session_id), {
        credentials: { password: liveSession.vnc_password },
      });
      rfb.scaleViewport = false;
      rfb.resizeSession = false;
      rfb.addEventListener("connect", () => {
        if (!cancelled) setLiveStatus({ kind: "info", text: "Connected - click the canvas to focus it, then use magic normally." });
      });
      rfb.addEventListener("disconnect", (e) => {
        if (cancelled) return;
        const clean = e.detail?.clean;
        setLiveStatus({
          kind: clean ? "info" : "error",
          text: clean ? "Session disconnected." : "Live magic connection lost unexpectedly.",
        });
      });
      rfb.addEventListener("securityfailure", (e) => {
        if (!cancelled) {
          setLiveStatus({
            kind: "error",
            text: `VNC authentication failed: ${e.detail?.reason || "unknown reason"}`,
          });
        }
      });
      createdRfb = rfb;
      rfbRef.current = rfb;
    });
    return () => {
      cancelled = true;
      if (createdRfb) createdRfb.disconnect();
    };
  }, [liveSession]);

  async function openSessionOn(library, cell) {
    setLiveStarting(true);
    setLiveStatus(null);
    try {
      const session = await startLayoutCellSession(library, cell, startResolution);
      setLiveSession(session);
      if (session.reused) {
        setLiveStatus({ kind: "info", text: "Reusing the magic session already open for this cell." });
      }
    } catch (e) {
      setLiveStatus({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setLiveStarting(false);
    }
  }

  async function handleStopLive() {
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
    const session = liveSession;
    setLiveSession(null);
    setLiveStatus(null);
    if (session) {
      try {
        await stopLayoutSession(session.session_id);
      } catch (e) {
        setLiveStatus({ kind: "error", text: `Session may still be running: ${e instanceof Error ? e.message : String(e)}` });
      }
    }
  }

  function handleViewSizeChange(size) {
    setViewSize(size);
    saveViewSize(size);
  }

  function handleToggleFullscreen() {
    if (!canvasHostRef.current) return;
    if (document.fullscreenElement === canvasHostRef.current) {
      document.exitFullscreen?.();
    } else {
      canvasHostRef.current.requestFullscreen?.().catch(() => {
        setLiveStatus({ kind: "error", text: "This browser blocked fullscreen for the canvas." });
      });
    }
  }

  function handleStartResolutionChange(value) {
    setStartResolution(value);
    saveStartResolution(value);
  }

  async function handleApplyResolution() {
    if (!liveSession || !resizeChoice || resizeChoice === liveSession.resolution) return;
    const confirmed = window.confirm(
      `Change the display to ${resizeChoice}? This restarts the magic session - any unsaved ` +
        "work in the current window (not yet saved with magic's `save` command) will be LOST. " +
        "This cannot be undone."
    );
    if (!confirmed) return;
    setResizeBusy(true);
    setResizeMsg(null);
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
    try {
      const session = await restartLayoutSession(liveSession.session_id, resizeChoice);
      setLiveSession(session);
      setLiveStatus({ kind: "info", text: `Session restarted at ${resizeChoice} - reconnecting...` });
    } catch (e) {
      setResizeMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setResizeBusy(false);
    }
  }

  async function handleCreateLibrary() {
    const name = newLibName.trim();
    if (!name) return;
    setLibBusy(true);
    setLibError(null);
    try {
      await createLibrary(name);
      setLibraries((libs) =>
        libs.some((l) => l.name === name)
          ? libs
          : [...libs, { name, cells: [] }].sort((a, b) => a.name.localeCompare(b.name))
      );
      setLibChoice(name);
      setNewLibName("");
    } catch (e) {
      setLibError(e instanceof Error ? e.message : String(e));
    } finally {
      setLibBusy(false);
    }
  }

  function isNewCellReady() {
    return Boolean(libChoice && libChoice !== NEW_LIBRARY && cellName.trim());
  }

  async function handleCapture() {
    if (!resolved?.found) return;
    setCapturing(true);
    setCaptureMsg(null);
    try {
      const result = await captureLayout(resolved.library, resolved.cell);
      setCaptureMsg({ kind: "info", text: "Captured: layout rendered to PNG." });
      setResolved((r) => (r ? { ...r, png: result.png, png_url: result.png_url } : r));
    } catch (e) {
      setCaptureMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setCapturing(false);
    }
  }

  async function handleCreateAndOpen() {
    const cell = cellName.trim();
    if (!isNewCellReady()) return;
    setCreating(true);
    setCreateMsg(null);
    try {
      const created = await createBlankLayout(libChoice, cell, topology, topologyLabel || topology);
      setCreateMsg({
        kind: "info",
        text: `Created ${created.library}/${created.cell} — opening a live magic session on it now.`,
      });
      await openSessionOn(created.library, created.cell);
      setResolved({ found: true, library: created.library, cell: created.cell, mag: created.mag, dir: created.dir });
    } catch (e) {
      if (e instanceof LayoutExistsError) {
        setCreateMsg({ kind: "error", text: `${e.message}. Opening the existing layout instead.` });
        await openSessionOn(e.library, e.cell);
        setResolved({ found: true, library: e.library, cell: e.cell });
      } else {
        setCreateMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      }
    } finally {
      setCreating(false);
    }
  }

  if (!topology) return null;

  const hasLibraryCell = Boolean(resolved?.found);
  const liveAvailable = prereqs?.available === true;

  function renderResolutionPicker(disabled) {
    if (!resolutions) return null;
    return (
      <label
        className="xschem-resolution-picker"
        title="Xvfb display size a NEW live session starts at - bigger gives magic more real drawing area, not just a bigger picture. Does not affect an already-running session."
      >
        Display size
        <select
          value={startResolution || resolutions.default}
          onChange={(e) => handleStartResolutionChange(e.target.value)}
          disabled={disabled}
        >
          {resolutions.allowed.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
    );
  }

  return (
    <div className="schematic-panel layout-panel">
      <div className="schematic-panel-head">
        <h3>
          Layout — {topologyLabel || topology}
          {blockId ? <span className="schematic-block-id"> ({blockId})</span> : null}
        </h3>
        <button type="button" className="toggle-raw collapse-btn" onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </div>

      {!collapsed && (
        <div className="schematic-panel-body">
          {loading && <p className="hint">Looking up the layout for this block...</p>}
          {error && <div className="error-box small">{error}</div>}

          {!loading && !error && !hasLibraryCell && (
            <div className="schematic-empty-state">
              <p className="schematic-empty">{resolved?.message || "No layout yet for this cell."}</p>
              <div className="new-schematic-form">
                <p className="hint">
                  Creates a blank magic layout in a design library cell and opens it live right
                  here. Once you've drawn something and saved it in magic, use "Render preview" to
                  see a static PNG here too.
                </p>
                <div className="library-picker">
                  <label>
                    Design library
                    <select
                      value={libChoice}
                      onChange={(e) => {
                        setLibChoice(e.target.value);
                        setLibError(null);
                      }}
                      disabled={schResolved?.found && schResolved.source === "library"}
                    >
                      <option value="" disabled>
                        Select a library...
                      </option>
                      {libraries.map((l) => (
                        <option key={l.name} value={l.name}>
                          {l.name}
                          {l.cells?.length ? ` (${l.cells.length} cell${l.cells.length > 1 ? "s" : ""})` : ""}
                        </option>
                      ))}
                      <option value={NEW_LIBRARY}>+ Create new library...</option>
                    </select>
                  </label>
                  {libChoice === NEW_LIBRARY && (
                    <div className="field-row new-library-row">
                      <label>
                        New library name
                        <input
                          type="text"
                          placeholder="e.g. ddr_afe"
                          value={newLibName}
                          onChange={(e) => setNewLibName(e.target.value)}
                        />
                      </label>
                      <button type="button" onClick={handleCreateLibrary} disabled={libBusy || !newLibName.trim()}>
                        {libBusy ? "Creating..." : "Create library"}
                      </button>
                    </div>
                  )}
                  {libChoice && libChoice !== NEW_LIBRARY && (
                    <label>
                      Cell name
                      <input
                        type="text"
                        value={cellName}
                        onChange={(e) => setCellName(e.target.value)}
                        disabled={schResolved?.found && schResolved.source === "library"}
                      />
                    </label>
                  )}
                  {libError && <p className="field-warning">{libError}</p>}
                </div>
                {renderResolutionPicker(creating || liveStarting)}
                <button type="button" onClick={handleCreateAndOpen} disabled={creating || liveStarting || !isNewCellReady()}>
                  {creating || liveStarting ? "Creating..." : "Create layout in magic"}
                </button>
                {createMsg && <p className={createMsg.kind === "error" ? "field-warning" : "hint"}>{createMsg.text}</p>}
                {prereqs !== null && !liveAvailable && (
                  <p className="hint schematic-no-display">{prereqs.message}</p>
                )}
              </div>
            </div>
          )}

          {!loading && !error && hasLibraryCell && !liveSession && (
            <>
              {resolved.png_url ? (
                <img
                  className="schematic-img"
                  src={apiUrl(resolved.png_url)}
                  alt={`Rendered layout for ${topologyLabel || topology}`}
                />
              ) : (
                <p className="hint">
                  No rendered preview yet - click "Render preview" below, or open the live session
                  to view/edit directly.
                </p>
              )}
              <p className="hint schematic-source">
                Layout filed at <code>{resolved.library}/{resolved.cell}</code>
                {resolved.mag ? ` (${resolved.mag})` : ""}
              </p>
              <div className="schematic-actions">
                <button type="button" onClick={handleCapture} disabled={capturing}>
                  {capturing ? "Rendering..." : resolved.png_url ? "Refresh preview" : "Render preview"}
                </button>
              </div>
              {captureMsg && <p className={captureMsg.kind === "error" ? "field-warning" : "hint"}>{captureMsg.text}</p>}
              <div className="schematic-actions xschem-live-actions">
                {renderResolutionPicker(liveStarting || (prereqs !== null && !liveAvailable))}
                <button
                  type="button"
                  onClick={() => openSessionOn(resolved.library, resolved.cell)}
                  disabled={liveStarting || (prereqs !== null && !liveAvailable)}
                  title={
                    prereqs !== null && !liveAvailable
                      ? prereqs.message
                      : "Open this layout in an interactive magic session embedded right here in the browser"
                  }
                >
                  {liveStarting ? "Starting..." : "Edit in magic (live, in browser)"}
                </button>
                {prereqs !== null && !liveAvailable && <span className="hint schematic-no-display">{prereqs.message}</span>}
              </div>
            </>
          )}

          {!loading && !error && liveSession && (
            <div className="xschem-live-session">
              <p className="hint schematic-source">
                Live session — display {liveSession.display} ({liveSession.resolution})
                {liveSession.reused ? " (reused)" : ""} — {liveSession.clients ?? 0} browser client(s) connected
              </p>

              <div
                className="xschem-view-controls"
                title="Rescales the picture already being streamed - instant, no server round-trip, does NOT give magic more drawing area."
              >
                <span className="xschem-view-controls-label">View size:</span>
                {VIEW_SIZES.map((size) => (
                  <button
                    key={size}
                    type="button"
                    className={viewSize === size ? "view-size-btn active" : "view-size-btn"}
                    onClick={() => handleViewSizeChange(size)}
                  >
                    {VIEW_SIZE_LABELS[size]}
                  </button>
                ))}
                <button type="button" className="view-size-btn" onClick={handleToggleFullscreen}>
                  {isFullscreen ? "Exit fullscreen" : "Fullscreen"}
                </button>
              </div>

              <div
                className={`xschem-live-canvas size-${viewSize}${isFullscreen ? " is-fullscreen" : ""}`}
                ref={canvasHostRef}
              />

              <div className="schematic-actions xschem-live-actions">
                <button type="button" onClick={handleStopLive}>
                  Stop live session
                </button>
              </div>

              {resolutions && (
                <div
                  className="xschem-resize-row"
                  title="Changes magic's ACTUAL drawing area, not just the picture size - but requires killing and restarting this session (Xvfb can't resize live on this machine), so any unsaved magic work is lost."
                >
                  <label className="xschem-resolution-picker">
                    Display resolution
                    <select
                      value={resizeChoice || liveSession.resolution}
                      onChange={(e) => setResizeChoice(e.target.value)}
                      disabled={resizeBusy}
                    >
                      {resolutions.allowed.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    onClick={handleApplyResolution}
                    disabled={resizeBusy || !resizeChoice || resizeChoice === liveSession.resolution}
                  >
                    {resizeBusy ? "Restarting..." : "Apply (restarts session, unsaved work lost)"}
                  </button>
                </div>
              )}
              {resizeMsg && <p className={resizeMsg.kind === "error" ? "field-warning" : "hint"}>{resizeMsg.text}</p>}
            </div>
          )}
          {!loading && !error && liveStatus && (
            <p className={liveStatus.kind === "error" ? "field-warning" : "hint"}>{liveStatus.text}</p>
          )}
        </div>
      )}
    </div>
  );
}
