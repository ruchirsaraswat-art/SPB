import { useEffect, useRef, useState } from "react";
import {
  apiUrl,
  openTopologySchematic,
  resolveSchematic,
  startXschemSession,
  stopXschemSession,
  xschemPrereqs,
  xschemWsUrl,
} from "./api";

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
  }, [topology]);

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
            <p className="schematic-empty">{resolved.message}</p>
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

              <div className="schematic-actions xschem-live-actions">
                {!liveSession ? (
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
                ) : (
                  <button type="button" onClick={handleStopLive}>
                    Stop live session
                  </button>
                )}
                {prereqs !== null && !liveAvailable && !liveSession && (
                  <span className="hint schematic-no-display">{prereqs.message}</span>
                )}
              </div>

              {liveSession && (
                <div className="xschem-live-session">
                  <p className="hint schematic-source">
                    Live session — display {liveSession.display}
                    {liveSession.reused ? " (reused)" : ""} — {liveSession.clients ?? 0} browser client(s)
                    connected
                  </p>
                  <div className="xschem-live-canvas" ref={canvasHostRef} />
                </div>
              )}
              {liveStatus && (
                <p className={liveStatus.kind === "error" ? "field-warning" : "hint"}>{liveStatus.text}</p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
