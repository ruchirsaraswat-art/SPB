import { useEffect, useState } from "react";
import { apiUrl, openTopologySchematic, resolveSchematic } from "./api";

// Schematic sub-window (cycle 4): whenever a block is highlighted in the PHY
// diagram (or a topology picked from the dropdown), show the best schematic
// the tool already has for it - the backend resolves a filed library cell
// first, else the latest successful run's rendered PNG, else a defined
// empty state. Collapsible side panel next to the spec form; the PNG is the
// always-works default view, "Open in xschem" is the display-gated extra.
export default function SchematicPanel({ topology, topologyLabel, blockId }) {
  const [resolved, setResolved] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const [opening, setOpening] = useState(false);
  const [openMsg, setOpenMsg] = useState(null); // {kind: "info"|"error", text}

  useEffect(() => {
    if (!topology) {
      setResolved(null);
      return undefined;
    }
    let stale = false; // ignore a slow response after the user moved on to another block
    setLoading(true);
    setError(null);
    setOpenMsg(null);
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
            </>
          )}
        </div>
      )}
    </div>
  );
}
