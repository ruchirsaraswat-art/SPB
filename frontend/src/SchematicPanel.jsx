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
  startXschemCellSession,
  startXschemSession,
  stopXschemSession,
  xschemPrereqs,
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
      const session = await startXschemSession(topology);
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
      const session = await startXschemCellSession(library, cell);
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
              status line render the same way. */}
          {!loading && !error && liveSession && (
            <div className="xschem-live-session">
              <p className="hint schematic-source">
                Live session — display {liveSession.display}
                {liveSession.reused ? " (reused)" : ""} — {liveSession.clients ?? 0} browser client(s)
                connected
              </p>
              <div className="xschem-live-canvas" ref={canvasHostRef} />
              <div className="schematic-actions xschem-live-actions">
                <button type="button" onClick={handleStopLive}>
                  Stop live session
                </button>
              </div>
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
