import { useEffect, useRef, useState } from "react";
import {
  apiUrl,
  captureSchematic,
  CellExistsError,
  createBlankSchematic,
  createLibrary,
  listLibraries,
  openTopologySchematic,
  resolveSchematic,
  restartXschemSession,
  startXschemCellSession,
  startXschemSession,
  stopXschemSession,
  xschemPrereqs,
  xschemResolutions,
  xschemWsUrl,
} from "./api";

const NEW_LIBRARY = "__new__"; // same sentinel SpecForm's library picker uses

// Sanitize a block id / topology value into a valid library/cell name (same
// charset backend/libraries.py's validate_library_name enforces: letters,
// digits, '_'/'-', starting with a letter or '_', max 64 chars) - so seeding
// the cell name from the selected block never round-trips into a 422.
function sanitizeCellName(raw) {
  let s = (raw || "").toLowerCase().replace(/[^a-z0-9_-]/g, "_");
  if (!/^[a-z_]/.test(s)) s = `_${s}`;
  return s.slice(0, 64) || "new_cell";
}

// --- live-session resize (2026-09): two independent, clearly-separate ------
// levers - see the UI copy near their controls below for the user-facing
// explanation of the difference.
//   A. "View size" - CSS-only, instant, no backend call at all: how big the
//      EXISTING framebuffer is drawn in the browser (a drag-free
//      small/medium/large/fit-width preset, plus a real Fullscreen mode via
//      the browser's Fullscreen API). Never gives xschem more drawing area
//      - just rescales what's already there, aspect-ratio preserved (see
//      the CSS: width/height are never both forced, so the canvas's native
//      aspect ratio always wins - no distortion).
//   B. "Display resolution" - the actual Xvfb screen size xschem draws
//      into. Free to pick for a brand-new session; changing it on an
//      ALREADY-RUNNING session requires restartXschemSession(), which kills
//      and relaunches the whole session (Xvfb's RANDR can't resize a live
//      display on this machine) - destructive to anything unsaved in
//      xschem, so it's gated behind an explicit window.confirm() below.
const VIEW_SIZE_KEY = "xschem-live-view-size";
const START_RESOLUTION_KEY = "xschem-live-start-resolution";
const VIEW_SIZES = ["small", "medium", "large", "fit"];
const VIEW_SIZE_LABELS = { small: "Small", medium: "Medium", large: "Large", fit: "Fit width" };

function loadViewSize() {
  try {
    const v = localStorage.getItem(VIEW_SIZE_KEY);
    return VIEW_SIZES.includes(v) ? v : "fit";
  } catch {
    return "fit"; // localStorage unavailable (private-mode Safari etc.)
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

// Schematic sub-window (cycle 4): whenever a block is highlighted in the PHY
// diagram (or a topology picked from the dropdown), show the best schematic
// the tool already has for it - the backend resolves a filed library cell
// first, else the latest successful run's rendered PNG, else a defined
// empty state. Collapsible side panel next to the spec form; the PNG is the
// always-works default view.
//
// Two ways to edit that schematic interactively:
//   - "Open in xschem (this machine)" - the pre-existing display-gated
//     action: spawns xschem on the BACKEND's own X display. Only useful
//     sitting at that machine.
//   - "Edit in xschem (live, in browser)" (2026-09 remote-access spec) - a
//     per-session Xvfb + xschem + x11vnc, streamed into an embedded noVNC
//     canvas right here, reachable from anywhere the tool itself is reachable
//     (e.g. over Tailscale) because it rides the same authenticated
//     WebSocket proxy as every other API call - see backend/xschem_session.py
//     and backend/main.py's /api/xschem/* routes.
export default function SchematicPanel({ topology, topologyLabel, blockId }) {
  const [resolved, setResolved] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const [opening, setOpening] = useState(false);
  const [openMsg, setOpenMsg] = useState(null); // {kind: "info"|"error", text}

  // --- live (browser-embedded) xschem session state -------------------------
  const [prereqs, setPrereqs] = useState(null); // {available, missing, message}
  const [liveSession, setLiveSession] = useState(null); // {session_id, vnc_password, display, ...}
  const [liveStarting, setLiveStarting] = useState(false);
  const [liveStatus, setLiveStatus] = useState(null); // {kind: "info"|"error", text}
  const rfbRef = useRef(null);
  const canvasHostRef = useRef(null);

  // --- resize state (2026-09) - see the two-levers comment above --------
  const [resolutions, setResolutions] = useState(null); // {allowed: [...], default}
  const [startResolution, setStartResolution] = useState(""); // picked for the NEXT new session
  const [viewSize, setViewSize] = useState(loadViewSize); // "small"|"medium"|"large"|"fit", CSS-only
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [resizeChoice, setResizeChoice] = useState(""); // dropdown value for restarting a LIVE session
  const [resizeBusy, setResizeBusy] = useState(false);
  const [resizeMsg, setResizeMsg] = useState(null); // {kind, text}

  // --- draw-a-new-schematic-by-hand state (empty-state action) --------------
  // Reuses the exact library/cell picker convention SpecForm.jsx already has
  // (fetched libraries list, inline "create new library") rather than a
  // parallel UI - just scoped to this panel's own empty-state form.
  const [libraries, setLibraries] = useState([]);
  const [libChoice, setLibChoice] = useState(""); // "" | existing name | NEW_LIBRARY
  const [newLibName, setNewLibName] = useState("");
  const [libBusy, setLibBusy] = useState(false);
  const [libError, setLibError] = useState(null);
  const [cellName, setCellName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createMsg, setCreateMsg] = useState(null); // {kind, text}
  // The cell this panel just created (or is opening a live session on) but
  // hasn't been captured yet - resolveSchematic() still reports found:false
  // for it (no PNG yet), so the panel tracks it separately from `resolved`.
  const [manualCell, setManualCell] = useState(null); // {library, cell}
  const [capturing, setCapturing] = useState(false);
  const [captureMsg, setCaptureMsg] = useState(null); // {kind, text}

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
    xschemPrereqs()
      .then((p) => {
        if (!stale) setPrereqs(p);
      })
      .catch((e) => {
        if (!stale) {
          setPrereqs({
            available: false,
            message: e instanceof Error ? e.message : "Could not check xschem prerequisites.",
          });
        }
      });
    return () => {
      stale = true;
    };
  }, []);

  useEffect(() => {
    let stale = false;
    xschemResolutions()
      .then((r) => {
        if (stale) return;
        setResolutions(r);
        setStartResolution((cur) => cur || loadStartResolution(r.default));
      })
      .catch(() => {
        // Non-fatal: the "Edit in xschem" buttons still work without a
        // picker - start_session()'s own DEFAULT_RESOLUTION applies.
      });
    return () => {
      stale = true;
    };
  }, []);

  // Track real browser Fullscreen state (not just "we asked for it") so the
  // toggle button's label stays correct even if the user exits with Esc
  // instead of clicking it again.
  useEffect(() => {
    function onFsChange() {
      setIsFullscreen(document.fullscreenElement === canvasHostRef.current);
    }
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  // Keep the "change resolution" dropdown defaulted to whatever the live
  // session is ACTUALLY running at, so it always starts as a no-op choice.
  useEffect(() => {
    setResizeChoice(liveSession?.resolution || "");
    setResizeMsg(null);
  }, [liveSession?.session_id, liveSession?.resolution]);

  useEffect(() => {
    if (!topology) {
      setResolved(null);
      return undefined;
    }
    let stale = false; // ignore a slow response after the user moved on to another block
    setLoading(true);
    setError(null);
    setOpenMsg(null);
    // Switching to a different block/topology: tear down any live session's
    // RFB connection for the PREVIOUS block (the backend session itself is
    // left running - the idle reaper cleans it up if nobody comes back).
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
    setLiveSession(null);
    setLiveStatus(null);
    // A different block also invalidates any in-progress "create a new
    // schematic" state from the PREVIOUS block's empty state.
    setLibChoice("");
    setNewLibName("");
    setLibError(null);
    setCellName(sanitizeCellName(blockId || topology));
    setCreateMsg(null);
    setManualCell(null);
    setCaptureMsg(null);
    resolveSchematic(topology)
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

  // Disconnect the RFB client on unmount (component removed / focus mode
  // hid the workspace this panel lives in) - never leaves a dangling
  // WebSocket open in the browser. The backend session survives (idle
  // reaper handles it) so coming back reuses it via startXschemSession.
  useEffect(() => {
    return () => {
      if (rfbRef.current) {
        rfbRef.current.disconnect();
        rfbRef.current = null;
      }
    };
  }, []);

  // Mount the noVNC client once a session exists and its canvas host div is
  // rendered. noVNC's `core/` tree (vendored verbatim under
  // frontend/public/novnc/core/ - see that directory's LICENSE.txt/AUTHORS)
  // is genuine browser-native ES modules (uses top-level await), which the
  // npm @novnc/novnc package's CJS-transpiled `lib/` build does NOT bundle
  // cleanly through Vite's rolldown bundler (require() of a module with
  // top-level await is a hard error) - loading it as a real runtime URL
  // instead of a bundled import sidesteps that entirely, and is also
  // noVNC's own documented "no build step" usage pattern. `@vite-ignore`
  // tells Vite not to try to statically analyze/bundle this - it's a plain
  // static asset (served from public/, so present unmodified in dist/ too -
  // see backend/main.py's single-origin SPA fallback).
  useEffect(() => {
    if (!liveSession || !canvasHostRef.current) return undefined;
    let cancelled = false;
    let createdRfb = null;
    // The URL is built at runtime (not a literal in the import() call) so
    // Vite/rolldown never tries to statically resolve+bundle it at build
    // time - it's a plain static asset (served from public/, so present
    // unmodified in dist/ too - see backend/main.py's single-origin SPA
    // fallback), meant to be loaded as a real browser-native module URL,
    // exactly like noVNC's own documented no-build-step usage.
    const novncRfbUrl = ["", "novnc", "core", "rfb.js"].join("/");
    import(/* @vite-ignore */ novncRfbUrl).then((mod) => {
      if (cancelled || !canvasHostRef.current) return;
      const RFB = mod.default;
      const rfb = new RFB(canvasHostRef.current, xschemWsUrl(liveSession.session_id), {
        credentials: { password: liveSession.vnc_password },
      });
      // NOT rfb.scaleViewport = true: noVNC's own JS-driven viewport scaling
      // sets the canvas's INLINE style.width/height from its container's
      // measured size at connect time, and empirically (2026-09) that
      // measurement can race the panel's own layout and land on 0x0 -
      // producing a canvas that has real painted pixels (confirmed via
      // canvas.getContext('2d').getImageData - non-zero bytes) but renders
      // as nothing but the container's black background because its CSS
      // size is 0px. Plain CSS scaling instead (see the
      // ".xschem-live-canvas canvas" rule in App.css: width:100%;
      // height:auto) has no such race - the browser lays it out exactly
      // like an <img>, deterministically, using the canvas's native
      // 1280x800 backing size.
      rfb.scaleViewport = false;
      rfb.resizeSession = false;
      rfb.addEventListener("connect", () => {
        if (!cancelled) setLiveStatus({ kind: "info", text: "Connected - click the canvas to focus it, then use xschem normally." });
      });
      rfb.addEventListener("disconnect", (e) => {
        if (cancelled) return;
        const clean = e.detail?.clean;
        setLiveStatus({
          kind: clean ? "info" : "error",
          text: clean ? "Session disconnected." : "Live xschem connection lost unexpectedly.",
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

  async function handleStartLive() {
    setLiveStarting(true);
    setLiveStatus(null);
    try {
      const session = await startXschemSession(topology, startResolution);
      setLiveSession(session);
      if (session.reused) {
        setLiveStatus({ kind: "info", text: "Reusing the xschem session already open for this block." });
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
        await stopXschemSession(session.session_id);
      } catch (e) {
        setLiveStatus({ kind: "error", text: `Session may still be running: ${e instanceof Error ? e.message : String(e)}` });
      }
    }
  }

  // --- resize handlers (2026-09) --------------------------------------------

  // Lever A: view size - pure CSS, no backend call, instant.
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

  // Lever B: display resolution - DESTRUCTIVE (kills and restarts the whole
  // session, discarding anything not saved in xschem with Ctrl+S) - always
  // confirm explicitly first, never silently.
  async function handleApplyResolution() {
    if (!liveSession || !resizeChoice || resizeChoice === liveSession.resolution) return;
    const confirmed = window.confirm(
      `Change the display to ${resizeChoice}? This restarts the xschem session - any unsaved ` +
        "work in the current window (not yet saved with Ctrl+S) will be LOST. This cannot be undone."
    );
    if (!confirmed) return;
    setResizeBusy(true);
    setResizeMsg(null);
    if (rfbRef.current) {
      rfbRef.current.disconnect();
      rfbRef.current = null;
    }
    try {
      const session = await restartXschemSession(liveSession.session_id, resizeChoice);
      setLiveSession(session);
      setLiveStatus({
        kind: "info",
        text: `Session restarted at ${resizeChoice} - reconnecting...`,
      });
    } catch (e) {
      setResizeMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setResizeBusy(false);
    }
  }

  // --- draw-a-new-schematic-by-hand handlers ---------------------------------

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

  // Starts a live session directly on a known library/cell (bypasses
  // resolveSchematic - a brand-new/not-yet-captured cell has no PNG, so
  // resolveSchematic would report it not found) and records it as the
  // panel's manualCell so the Capture action becomes available.
  async function openManualCellSession(library, cell) {
    setLiveStarting(true);
    setLiveStatus(null);
    try {
      const session = await startXschemCellSession(library, cell, startResolution);
      setManualCell({ library, cell });
      setLiveSession(session);
      if (session.reused) {
        setLiveStatus({ kind: "info", text: "Reusing the xschem session already open for this cell." });
      }
    } catch (e) {
      setLiveStatus({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setLiveStarting(false);
    }
  }

  async function handleCreateAndOpen() {
    const cell = cellName.trim();
    if (!isNewCellReady()) return;
    setCreating(true);
    setCreateMsg(null);
    try {
      const created = await createBlankSchematic(libChoice, cell, topology, topologyLabel || topology);
      setCreateMsg({
        kind: "info",
        text: `Created ${created.library}/${created.cell} — opening a live xschem session on it now.`,
      });
      await openManualCellSession(created.library, created.cell);
    } catch (e) {
      if (e instanceof CellExistsError) {
        setCreateMsg({
          kind: "error",
          text: `${e.message}. Opening the existing cell instead.`,
        });
        await openManualCellSession(e.library, e.cell);
      } else {
        setCreateMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
      }
    } finally {
      setCreating(false);
    }
  }

  // Just a readability helper for the disabled= expression below (kept as a
  // plain function rather than useMemo - this form is small and re-renders
  // cheaply).
  function isNewCellReady() {
    return Boolean(libChoice && libChoice !== NEW_LIBRARY && cellName.trim());
  }

  async function handleCapture() {
    if (!manualCell) return;
    setCapturing(true);
    setCaptureMsg(null);
    try {
      const result = await captureSchematic(manualCell.library, manualCell.cell);
      setCaptureMsg({
        kind: "info",
        text: result.netlist_warning
          ? `Rendered PNG captured; ${result.netlist_warning}`
          : "Captured: PNG and netlist extracted, this block now resolves to your schematic.",
      });
      // Re-resolve so the panel switches from the empty state to the
      // just-captured PNG, same as reselecting the block would.
      const r = await resolveSchematic(topology);
      setResolved(r);
    } catch (e) {
      setCaptureMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setCapturing(false);
    }
  }

  // Shared by both "start a new live session" entry points (the resolved-
  // schematic "Edit in xschem" button and the empty-state "Create schematic
  // in xschem" form) - lets the user pick the Xvfb display size a NEW
  // session comes up at. Never touches an already-running session (see
  // Lever B's "change resolution" row inside the live-session block below
  // for that, which IS destructive and gated behind a confirm()).
  function renderResolutionPicker(disabled) {
    if (!resolutions) return null;
    return (
      <label className="xschem-resolution-picker" title="Xvfb display size a NEW live session starts at - bigger gives xschem more real drawing area, not just a bigger picture. Does not affect an already-running session.">
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

  if (!topology) return null;

  const xschemAvailable = resolved?.xschem?.available === true;
  const xschemReason = resolved?.xschem?.reason;

  async function handleOpenXschem() {
    setOpening(true);
    setOpenMsg(null);
    try {
      const r = await openTopologySchematic(topology);
      setOpenMsg({
        kind: "info",
        text:
          r.status === "already_open"
            ? "An xschem window for this schematic is already open (reusing it)."
            : `xschem opened on display ${r.display}.`,
      });
    } catch (e) {
      setOpenMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setOpening(false);
    }
  }

  const hasSchView = Boolean(resolved?.found && resolved.sch);
  const liveAvailable = prereqs?.available === true;
  const liveDisabledReason = !hasSchView
    ? "this block's filed result has a rendered PNG but no .sch schematic view, so there is nothing for xschem to open"
    : prereqs?.available === false
      ? prereqs.message
      : null;

  const sourceLine =
    resolved?.found &&
    (resolved.source === "library"
      ? `Library cell ${resolved.library}/${resolved.cell}` +
        (resolved.filed_at ? `, filed ${resolved.filed_at.slice(0, 16).replace("T", " ")}` : "") +
        (resolved.run_id ? ` (run ${resolved.run_id})` : "")
      : `Latest successful run ${resolved.run_id}` +
        (resolved.created_at ? `, ${resolved.created_at.slice(0, 16).replace("T", " ")}` : ""));

  return (
    <div className="schematic-panel">
      <div className="schematic-panel-head">
        <h3>
          Schematic — {topologyLabel || topology}
          {blockId ? <span className="schematic-block-id"> ({blockId})</span> : null}
        </h3>
        <button
          type="button"
          className="toggle-raw collapse-btn"
          onClick={() => setCollapsed((c) => !c)}
        >
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </div>

      {!collapsed && (
        <div className="schematic-panel-body">
          {loading && <p className="hint">Looking up the latest schematic...</p>}
          {error && <div className="error-box small">{error}</div>}

          {!loading && !error && resolved && !resolved.found && (
            <div className="schematic-empty-state">
              <p className="schematic-empty">{resolved.message}</p>

              {!manualCell && (
                <div className="new-schematic-form">
                  <p className="hint">
                    Or draw one by hand: creates a blank schematic in a design library cell and
                    opens it live in xschem right here, with sky130 symbols reachable in its
                    library browser.
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
                        <button
                          type="button"
                          onClick={handleCreateLibrary}
                          disabled={libBusy || !newLibName.trim()}
                        >
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
                        />
                      </label>
                    )}
                    {libError && <p className="field-warning">{libError}</p>}
                  </div>
                  {renderResolutionPicker(creating || liveStarting)}
                  <button
                    type="button"
                    onClick={handleCreateAndOpen}
                    disabled={creating || liveStarting || !isNewCellReady()}
                  >
                    {creating || liveStarting ? "Creating..." : "Create schematic in xschem"}
                  </button>
                  {createMsg && (
                    <p className={createMsg.kind === "error" ? "field-warning" : "hint"}>{createMsg.text}</p>
                  )}
                </div>
              )}

              {manualCell && (
                <p className="hint schematic-source">
                  Drawing <code>{manualCell.library}/{manualCell.cell}</code> — save from xschem
                  (Ctrl+S), then Capture below to render it and make this block resolve to your
                  schematic.
                </p>
              )}
            </div>
          )}

          {!loading && !error && resolved?.found && (
            <>
              <img
                className="schematic-img"
                src={apiUrl(resolved.png_url)}
                alt={`Rendered schematic for ${topologyLabel || topology}`}
              />
              <p className="hint schematic-source">
                {sourceLine}
                {resolved.chosen_architecture ? ` — ${resolved.chosen_architecture}` : ""}
                {resolved.label ? ` — "${resolved.label}"` : ""}
              </p>
              <div className="schematic-actions">
                <button
                  type="button"
                  onClick={handleOpenXschem}
                  disabled={!xschemAvailable || opening}
                  title={
                    xschemAvailable
                      ? "Open this schematic interactively in xschem (uses the run/library xschemrc, sky130 symbols resolve)"
                      : xschemReason || "No X display available on the backend machine"
                  }
                >
                  {opening ? "Opening..." : "Open in xschem"}
                </button>
                {!xschemAvailable && (
                  <span className="hint schematic-no-display">
                    xschem needs an X display on the backend machine - button disabled (hover for
                    details).
                  </span>
                )}
              </div>
              {openMsg && (
                <p className={openMsg.kind === "error" ? "field-warning" : "hint"}>{openMsg.text}</p>
              )}

              {!liveSession && (
                <div className="schematic-actions xschem-live-actions">
                  {renderResolutionPicker(liveStarting || !hasSchView || (prereqs !== null && !liveAvailable))}
                  <button
                    type="button"
                    onClick={handleStartLive}
                    disabled={liveStarting || !hasSchView || (prereqs !== null && !liveAvailable)}
                    title={
                      liveDisabledReason ||
                      "Open this schematic in an interactive xschem session embedded right here in the " +
                        "browser - works remotely (e.g. over Tailscale), not just sitting at the backend machine"
                    }
                  >
                    {liveStarting ? "Starting..." : "Edit in xschem (live, in browser)"}
                  </button>
                  {prereqs !== null && !liveAvailable && (
                    <span className="hint schematic-no-display">{prereqs.message}</span>
                  )}
                </div>
              )}
            </>
          )}

          {/* Shared live-session canvas + Capture action: whichever path started it
              (the resolved-schematic "Edit in xschem" above, or the empty-state
              "Create schematic in xschem" form), the embedded noVNC canvas and its
              status line render the same way. Two independent resize levers:
              the "View size" row below is CSS-only and instant (Lever A); the
              "Display resolution" row is the destructive restart-to-resize
              lever (Lever B), gated behind an explicit confirm(). */}
          {!loading && !error && liveSession && (
            <div className="xschem-live-session">
              <p className="hint schematic-source">
                Live session — display {liveSession.display} ({liveSession.resolution})
                {liveSession.reused ? " (reused)" : ""} — {liveSession.clients ?? 0} browser client(s)
                connected
              </p>

              <div className="xschem-view-controls" title="Rescales the picture already being streamed - instant, no server round-trip, does NOT give xschem more drawing area.">
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
                <div className="xschem-resize-row" title="Changes xschem's ACTUAL drawing area, not just the picture size - but requires killing and restarting this session (Xvfb can't resize live on this machine), so any unsaved xschem work is lost.">
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
              {resizeMsg && (
                <p className={resizeMsg.kind === "error" ? "field-warning" : "hint"}>{resizeMsg.text}</p>
              )}
            </div>
          )}
          {!loading && !error && liveStatus && (
            <p className={liveStatus.kind === "error" ? "field-warning" : "hint"}>{liveStatus.text}</p>
          )}
          {!loading && !error && manualCell && (
            <div className="schematic-actions">
              <button type="button" onClick={handleCapture} disabled={capturing}>
                {capturing ? "Capturing..." : "Capture into schematic (render PNG + netlist)"}
              </button>
            </div>
          )}
          {!loading && !error && captureMsg && (
            <p className={captureMsg.kind === "error" ? "field-warning" : "hint"}>{captureMsg.text}</p>
          )}
        </div>
      )}
    </div>
  );
}
