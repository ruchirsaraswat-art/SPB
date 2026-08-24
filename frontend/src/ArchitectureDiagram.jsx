import { useEffect, useMemo, useRef, useState } from "react";
import { afeArchStore } from "./phyArchitectures";

/**
 * Interactive wired block diagram of the complete PHY architecture. This IS
 * the sub-block selector (it replaces the old button grid in the "PHY
 * Architecture" area of the form):
 *   - click a block to design it
 *   - drag a block to rearrange the diagram
 *   - right-click a block to delete it (right-click a group to ungroup it)
 *   - click a wire to select it, then click the x that appears to delete it
 *   - "Add wire" then click a source block and a target block to connect them
 *   - "Add block" prompts for a name + topology and drops in a new block
 *   - "Merge blocks" lets the user click several blocks and merge them into
 *     a named group - a new hierarchy level holding those blocks
 *   - blocks with children show a corner [+] - click it to drill down into
 *     that block's own diagram; the breadcrumb above navigates back up
 * All edits persist per PHY type and hierarchy level in localStorage;
 * "Reset diagram" restores the current level's default architecture.
 *
 * Wires draw as straight horizontal/vertical lines whenever the two anchor
 * points are (nearly) aligned, and fall back to a diagonal only when the
 * blocks genuinely aren't aligned.
 *
 * Block coloring shows maturity: to-do -> selected -> exploring -> building
 * -> built (or failed). PAD nodes and other non-designable blocks are drawn
 * muted. "Save PNG" rasterizes the current level's diagram.
 *
 * `blockStates`: block id -> { status, detail } (see App.jsx). Block ids are
 * used as-is at every hierarchy level, so run history follows a block even
 * when it's merged into a group.
 */

const STATUS_STYLE = {
  todo: { fill: "#eceff4", stroke: "#9aa5b1", text: "#52606d", label: "not started" },
  selected: { fill: "#dbeafe", stroke: "#2563eb", text: "#1e3a8a", label: "specifying" },
  exploring: { fill: "#fef3c7", stroke: "#d97706", text: "#92400e", label: "exploring architectures" },
  building: { fill: "#ede9fe", stroke: "#7c3aed", text: "#4c1d95", label: "building" },
  built: { fill: "#dcfce7", stroke: "#16a34a", text: "#14532d", label: "built + simulated" },
  failed: { fill: "#fee2e2", stroke: "#dc2626", text: "#7f1d1d", label: "last build failed" },
};

const PAD_STYLE = { fill: "#fff7ed", stroke: "#b45309", text: "#7c2d12" };
const GROUP_STYLE = { fill: "#f1f5f9", stroke: "#475569", text: "#1e293b" };
// Digital view accent: designable digital blocks are teal (vs the AFE view's
// grey/status palette) and AFE/core boundary anchors (kind "iface") indigo -
// same rounded "pad" treatment, instantly distinguishable from the AFE view.
const DIGITAL_BLOCK_STYLE = { fill: "#f0fdfa", stroke: "#0d9488", text: "#134e4a" };
const IFACE_STYLE = { fill: "#eef2ff", stroke: "#4f46e5", text: "#312e81" };
// Firmware view accent (2026-08-23 firmware-section spec): amber/orange -
// firmware blocks are SOFTWARE, so designable blocks also get a dashed border
// and a small "SW" corner tag (echoing the overview chain's convention), and
// selection deepens the amber instead of the blue the AFE/digital views use.
// Iface anchors keep the indigo IFACE_STYLE, identical to the digital view.
const FIRMWARE_BLOCK_STYLE = { fill: "#fffbeb", stroke: "#d97706", text: "#78350f" };
const FIRMWARE_SELECTED_STYLE = { fill: "#fde68a", stroke: "#b45309", text: "#78350f" };

const BLOCK_W = 148;
const BLOCK_H = 64;
const GAP = 44; // horizontal gap between grid columns
const ROW_GAP = 58; // vertical gap between grid rows (leaves room for status text)
const PAD_MARGIN = 16;
const TOP = 30;
const DRAG_SPACE = 0; // no reserved empty area - the canvas grows on demand while dragging
const CLICK_TOLERANCE = 5; // px of movement below which pointerup = click
const ALIGN_SNAP = 16; // anchors closer than this snap to a straight wire

// Where a wire attaches to each block: pick the facing sides based on the
// blocks' relative positions, so an edge to a block on the left/above doesn't
// slice through the box. Nearly-aligned anchors snap to a perfectly straight
// horizontal/vertical line; only genuinely misaligned blocks get a diagonal.
function wireAnchors(a, b) {
  const acx = a.x + BLOCK_W / 2;
  const acy = a.y + BLOCK_H / 2;
  const bcx = b.x + BLOCK_W / 2;
  const bcy = b.y + BLOCK_H / 2;
  const dx = bcx - acx;
  const dy = bcy - acy;
  let p;
  if (Math.abs(dx) >= Math.abs(dy)) {
    p =
      dx >= 0
        ? { x1: a.x + BLOCK_W, y1: acy, x2: b.x, y2: bcy }
        : { x1: a.x, y1: acy, x2: b.x + BLOCK_W, y2: bcy };
    if (Math.abs(p.y1 - p.y2) <= ALIGN_SNAP) {
      const y = (p.y1 + p.y2) / 2;
      p.y1 = y;
      p.y2 = y;
    }
  } else {
    p =
      dy >= 0
        ? { x1: acx, y1: a.y + BLOCK_H, x2: bcx, y2: b.y }
        : { x1: acx, y1: a.y, x2: bcx, y2: b.y + BLOCK_H };
    if (Math.abs(p.x1 - p.x2) <= ALIGN_SNAP) {
      const x = (p.x1 + p.x2) / 2;
      p.x1 = x;
      p.x2 = x;
    }
  }
  return p;
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function connKey(c) {
  return `${c.from}->${c.to}`;
}

function slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
}

const NO_WIRE_EDITS = { removed: [], added: [] };

// `version`: bumped by the parent whenever something OUTSIDE this component
// edited the architecture records in localStorage (chat patch apply/undo,
// saved-architecture load) - forces a re-read of blocks and wire edits.
// `store`: which arch store (namespace + built-in archMap) backs the diagram -
// afeArchStore (default, "arch-" keys) or digitalArchStore ("dig-arch-").
// `variant`: "afe" (default), "digital" or "firmware" - the digital view
// gets the teal block accent, no build-status captions/legend, and a
// select-only hint (digital blocks are never fed to the spec-form/build
// flow). "firmware" (2026-08-23 firmware-section spec) behaves exactly like
// "digital" (select-only, iface anchors, no build captions/legend - firmware
// blocks are never built through this diagram either) but carries a distinct
// amber/orange accent (FIRMWARE_BLOCK_STYLE, dashed borders + "SW" tag, amber
// selection) - `data-variant` on the wrapper is the CSS hook for the canvas tint.
// `topologyGroups`: optional [{group, options:[{value,label}]}] - renders the
// "Add block" topology picker with optgroups (used by the digital catalog);
// falls back to the flat `topologyOptions` list.
export default function ArchitectureDiagram({
  phyType,
  blockStates = {},
  selectedBlock,
  onSelectBlock,
  topologyOptions = [],
  version = 0,
  store = afeArchStore,
  variant = "afe",
  topologyGroups = null,
}) {
  // "digital" and "firmware" are both select-only control-plane views (no
  // build flow, iface anchors, no status captions/legend).
  const isDigital = variant === "digital" || variant === "firmware";
  const isFirmware = variant === "firmware";
  // Selection/connect highlight stroke: amber in the firmware view, blue elsewhere.
  const accentStroke = isFirmware ? "#b45309" : "#2563eb";
  const svgRef = useRef(null);
  const dragRef = useRef(null);
  const [path, setPath] = useState([]); // hierarchy: ancestor block ids, [] = top
  const [offsets, setOffsets] = useState({}); // block id -> {dx, dy}
  // Wire edits on top of this level's default edges: default wires the user
  // deleted (by key), and wires the user drew manually ({from, to}).
  const [wireEdits, setWireEdits] = useState(NO_WIRE_EDITS);
  const [selectedConn, setSelectedConn] = useState(null); // wire key, or null
  const [connectMode, setConnectMode] = useState(false);
  const [connectSource, setConnectSource] = useState(null); // block id, or null
  // "Add block" prompt + "Merge blocks" grouping state. Added/deleted blocks
  // live in localStorage per level; blocksVersion forces a re-read after an
  // edit so the change appears immediately.
  const [blocksVersion, setBlocksVersion] = useState(0);
  const [addMode, setAddMode] = useState(false);
  const [newBlockName, setNewBlockName] = useState("");
  const [newBlockTopo, setNewBlockTopo] = useState("");
  const [groupMode, setGroupMode] = useState(false);
  const [groupSel, setGroupSel] = useState([]); // block ids picked for merging
  const [groupName, setGroupName] = useState("");

  const layoutKey = store.layoutKeyForPhy(phyType, path);
  const wiresKey = store.wireEditsKey(phyType, path);

  useEffect(() => {
    setPath([]); // changing PHY type always starts back at the top level
  }, [phyType]);

  useEffect(() => {
    try {
      setOffsets(JSON.parse(localStorage.getItem(layoutKey)) || {});
    } catch {
      setOffsets({});
    }
    try {
      const stored = JSON.parse(localStorage.getItem(wiresKey));
      setWireEdits(stored && Array.isArray(stored.removed) && Array.isArray(stored.added) ? stored : NO_WIRE_EDITS);
    } catch {
      setWireEdits(NO_WIRE_EDITS);
    }
    setSelectedConn(null);
    setConnectMode(false);
    setConnectSource(null);
    setAddMode(false);
    setGroupMode(false);
    setGroupSel([]);
  }, [layoutKey, wiresKey, version]);

  const pathStr = path.join("/");
  const blocks = useMemo(() => store.blocksForPhy(phyType, path), [store, phyType, pathStr, blocksVersion, version]); // eslint-disable-line react-hooks/exhaustive-deps

  // Breadcrumb labels: walk down the hierarchy resolving each ancestor.
  const crumbs = useMemo(() => {
    const out = [];
    for (let i = 0; i < path.length; i++) {
      const blk = store.blocksForPhy(phyType, path.slice(0, i)).find((b) => b.id === path[i]);
      out.push({ id: path[i], label: blk?.short || path[i] });
    }
    return out;
  }, [store, phyType, pathStr, blocksVersion, version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!phyType || blocks.length === 0) {
    if (path.length > 0) {
      // an emptied-out child level - still show the breadcrumb so the user
      // can navigate back up (and Add block to populate it)
    } else {
      return null;
    }
  }

  // Actual (grid base + user-dragged offset) top-left corner of each block,
  // clamped so a block can never leave the canvas on the top/left. Base
  // positions come from each block's col/row in the level definition.
  const pos = {};
  blocks.forEach((b, i) => {
    const o = offsets[b.id] || { dx: 0, dy: 0 };
    pos[b.id] = {
      x: Math.max(8, PAD_MARGIN + (b.col ?? i) * (BLOCK_W + GAP) + o.dx),
      y: Math.max(8, TOP + (b.row ?? 0) * (BLOCK_H + ROW_GAP) + o.dy),
    };
  });

  // The canvas grows to fit wherever blocks have been dragged, so a block
  // moved right/down extends the drawing instead of getting clipped away.
  const maxCol = Math.max(0, ...blocks.map((b, i) => b.col ?? i));
  const maxRow = Math.max(0, ...blocks.map((b) => b.row ?? 0));
  const baseWidth = PAD_MARGIN * 2 + (maxCol + 1) * BLOCK_W + maxCol * GAP;
  const baseHeight = TOP + (maxRow + 1) * BLOCK_H + maxRow * ROW_GAP + 46 + DRAG_SPACE + 24;
  const width = Math.max(baseWidth, ...blocks.map((b) => pos[b.id].x + BLOCK_W + PAD_MARGIN));
  const height = Math.max(baseHeight, ...blocks.map((b) => pos[b.id].y + BLOCK_H + 46 + 24));

  // Wires actually drawn: this level's signal-flow edges minus deletions,
  // plus manual connections (skipping any whose blocks don't exist - deleted
  // blocks take their wires with them).
  const defaultConns = store.edgesForPhy(phyType, path);
  const conns = [];
  const seen = new Set();
  for (const c of [...defaultConns.filter((c) => !wireEdits.removed.includes(connKey(c))), ...wireEdits.added]) {
    const key = connKey(c);
    if (seen.has(key) || !pos[c.from] || !pos[c.to]) continue;
    seen.add(key);
    conns.push({ ...c, key });
  }

  // The digital view has no build flow - no status captions, no legend.
  const usedStatuses = isDigital
    ? []
    : [...new Set(blocks.filter((b) => b.topology).map((b) => (blockStates[b.id] || {}).status || "todo"))];

  function bumpBlocks() {
    setBlocksVersion((v) => v + 1);
  }

  function saveWireEdits(next) {
    setWireEdits(next);
    try {
      localStorage.setItem(wiresKey, JSON.stringify(next));
    } catch {
      // localStorage unavailable - wire edits just won't persist
    }
  }

  function deleteConn(key) {
    const isAdded = wireEdits.added.some((c) => connKey(c) === key);
    saveWireEdits(
      isAdded
        ? { ...wireEdits, added: wireEdits.added.filter((c) => connKey(c) !== key) }
        : { ...wireEdits, removed: [...wireEdits.removed, key] }
    );
    setSelectedConn(null);
  }

  function addConn(from, to) {
    const key = connKey({ from, to });
    if (seen.has(key)) return; // already drawn
    // Re-adding a deleted default wire = un-delete it; anything else is new.
    saveWireEdits(
      wireEdits.removed.includes(key)
        ? { ...wireEdits, removed: wireEdits.removed.filter((k) => k !== key) }
        : { ...wireEdits, added: [...wireEdits.added, { from, to }] }
    );
  }

  function saveUserBlocks(list) {
    try {
      localStorage.setItem(store.userBlocksKey(phyType, path), JSON.stringify(list));
    } catch {
      // localStorage unavailable
    }
  }

  function saveHiddenBlocks(list) {
    try {
      localStorage.setItem(store.hiddenBlocksKey(phyType, path), JSON.stringify(list));
    } catch {
      // localStorage unavailable
    }
  }

  function uniqueUserId(name) {
    const existing = new Set(blocks.map((b) => b.id));
    let id = `user-${slugify(name) || "block"}`;
    for (let n = 2; existing.has(id); n++) id = `user-${slugify(name)}-${n}`;
    return id;
  }

  function addBlock() {
    const name = newBlockName.trim();
    if (!name || !newBlockTopo) return;
    const block = {
      id: uniqueUserId(name),
      label: name,
      short: truncate(name, 16),
      topology: newBlockTopo,
      col: 0,
      row: maxRow + 1, // drops in below the diagram - drag it into place
      user: true,
    };
    saveUserBlocks([...store.userBlocksForPhy(phyType, path), block]);
    bumpBlocks();
    setAddMode(false);
    setNewBlockName("");
  }

  // Merge the picked blocks into a named group: a new user block whose
  // children hold the members and their internal wires. Wires that crossed
  // the group boundary are re-pointed at the group block itself.
  function createGroup() {
    const name = groupName.trim();
    if (!name || groupSel.length < 2) return;
    const sel = new Set(groupSel);
    const members = blocks.filter((b) => sel.has(b.id));
    const minCol = Math.min(...members.map((b, i) => b.col ?? i));
    const minRow = Math.min(...members.map((b) => b.row ?? 0));
    const group = {
      id: uniqueUserId(name),
      label: name,
      short: truncate(name, 16),
      topology: null,
      kind: "group",
      user: true,
      col: minCol,
      row: minRow,
      children: {
        blocks: members.map((b, i) => ({ ...b, col: (b.col ?? i) - minCol, row: (b.row ?? 0) - minRow })),
        edges: conns.filter((c) => sel.has(c.from) && sel.has(c.to)).map((c) => ({ from: c.from, to: c.to })),
      },
    };

    // Re-point boundary wires at the group; drop wires fully inside it.
    const remapped = [];
    const remapSeen = new Set();
    for (const c of conns) {
      const fromIn = sel.has(c.from);
      const toIn = sel.has(c.to);
      if (!fromIn && !toIn) continue;
      if (fromIn && toIn) continue; // internal - lives in group.children now
      const edge = { from: fromIn ? group.id : c.from, to: toIn ? group.id : c.to };
      const key = connKey(edge);
      if (edge.from === edge.to || remapSeen.has(key)) continue;
      remapSeen.add(key);
      remapped.push(edge);
    }
    saveWireEdits({
      removed: [
        ...wireEdits.removed,
        ...defaultConns.map(connKey).filter((k) => {
          const [f, t] = k.split("->");
          return (sel.has(f) || sel.has(t)) && !wireEdits.removed.includes(k);
        }),
      ],
      added: [
        ...wireEdits.added.filter((c) => !sel.has(c.from) && !sel.has(c.to)),
        ...remapped.filter((e) => !seen.has(connKey(e))),
      ],
    });

    // Members leave this level: user blocks are removed from the list,
    // default blocks are hidden.
    saveUserBlocks([...store.userBlocksForPhy(phyType, path).filter((b) => !sel.has(b.id)), group]);
    saveHiddenBlocks([...store.hiddenBlocksForPhy(phyType, path), ...members.filter((b) => !b.user).map((b) => b.id)]);
    bumpBlocks();
    setGroupMode(false);
    setGroupSel([]);
    setGroupName("");
  }

  // Right-click: delete a block; on a user-created group, ungroup instead -
  // members return to this level and the group's boundary wires vanish.
  function deleteBlock(block) {
    if (block.user && block.kind === "group") {
      const memberIds = new Set((block.children?.blocks || []).map((b) => b.id));
      saveUserBlocks([
        ...store.userBlocksForPhy(phyType, path).filter((b) => b.id !== block.id),
        ...(block.children?.blocks || []).filter((b) => b.user),
      ]);
      saveHiddenBlocks(store.hiddenBlocksForPhy(phyType, path).filter((id) => !memberIds.has(id)));
      // restore the default wires that were removed when the group formed
      saveWireEdits({
        removed: wireEdits.removed.filter((k) => {
          const [f, t] = k.split("->");
          return !memberIds.has(f) && !memberIds.has(t);
        }),
        added: wireEdits.added.filter((c) => c.from !== block.id && c.to !== block.id),
      });
    } else if (block.user) {
      saveUserBlocks(store.userBlocksForPhy(phyType, path).filter((b) => b.id !== block.id));
    } else {
      saveHiddenBlocks([...store.hiddenBlocksForPhy(phyType, path), block.id]);
    }
    if (connectSource === block.id) setConnectSource(null);
    setGroupSel((g) => g.filter((id) => id !== block.id));
    bumpBlocks();
  }

  function handleBlockClick(block) {
    if (groupMode) {
      setGroupSel((g) => (g.includes(block.id) ? g.filter((id) => id !== block.id) : [...g, block.id]));
      return;
    }
    if (connectMode) {
      if (!connectSource) {
        setConnectSource(block.id);
      } else if (connectSource === block.id) {
        setConnectSource(null); // clicking the source again cancels it
      } else {
        addConn(connectSource, block.id);
        setConnectSource(null);
        setConnectMode(false);
      }
      return;
    }
    if (block.topology && onSelectBlock) onSelectBlock(block);
  }

  function onPointerDown(e, id) {
    if (e.button !== 0) return; // right-click is delete/ungroup, not drag
    e.preventDefault();
    // Capture the screen->SVG scale once at drag start; the canvas can grow
    // mid-drag (it expands to fit the moved block), and recomputing the
    // scale from the live bounding box would make the block jitter/run away.
    const rect = svgRef.current.getBoundingClientRect();
    const scale = width / rect.width;
    const o = offsets[id] || { dx: 0, dy: 0 };
    dragRef.current = { id, startX: e.clientX, startY: e.clientY, scale, orig: o, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onPointerMove(e) {
    const d = dragRef.current;
    if (!d) return;
    const dx = (e.clientX - d.startX) * d.scale;
    const dy = (e.clientY - d.startY) * d.scale;
    if (!d.moved && Math.hypot(dx, dy) < CLICK_TOLERANCE) return;
    d.moved = true;
    setOffsets((prev) => ({ ...prev, [d.id]: { dx: d.orig.dx + dx, dy: d.orig.dy + dy } }));
  }

  function onPointerUp(e, block) {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) return;
    if (d.moved) {
      setOffsets((prev) => {
        try {
          localStorage.setItem(layoutKey, JSON.stringify(prev));
        } catch {
          // localStorage unavailable - layout just won't persist
        }
        return prev;
      });
    } else {
      handleBlockClick(block);
    }
  }

  function resetDiagram() {
    setOffsets({});
    setWireEdits(NO_WIRE_EDITS);
    setSelectedConn(null);
    setConnectMode(false);
    setConnectSource(null);
    setAddMode(false);
    setGroupMode(false);
    setGroupSel([]);
    try {
      localStorage.removeItem(layoutKey);
      localStorage.removeItem(wiresKey);
      localStorage.removeItem(store.userBlocksKey(phyType, path));
      localStorage.removeItem(store.hiddenBlocksKey(phyType, path));
      localStorage.removeItem(store.blockOverridesKey(phyType, path));
    } catch {
      // ignore
    }
    bumpBlocks();
  }

  function toggleConnectMode() {
    setConnectMode((m) => !m);
    setConnectSource(null);
    setSelectedConn(null);
    setGroupMode(false);
    setGroupSel([]);
  }

  function toggleGroupMode() {
    setGroupMode((m) => !m);
    setGroupSel([]);
    setGroupName("");
    setConnectMode(false);
    setConnectSource(null);
    setSelectedConn(null);
  }

  function savePng() {
    const svg = svgRef.current;
    if (!svg) return;
    const xml = new XMLSerializer().serializeToString(svg);
    const img = new Image();
    img.onload = () => {
      const scale = 2;
      const canvas = document.createElement("canvas");
      canvas.width = width * scale;
      canvas.height = height * scale;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const a = document.createElement("a");
      a.download = `${[...(variant !== "afe" ? [variant] : []), phyType, ...path].join("-")}-architecture.png`;
      a.href = canvas.toDataURL("image/png");
      a.click();
    };
    img.src = `data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(xml)))}`;
  }

  const hint = groupMode
    ? `Merge: click the blocks to include (${groupSel.length} picked), name the group, then "Create group"`
    : connectMode
    ? connectSource
      ? "Connect: now click the target block (or the source again to cancel)"
      : "Connect: click the source block"
    : isDigital
    ? `Click a block to highlight it (${variant} blocks aren't built through this diagram in v1) - drag to rearrange - right-click deletes (ungroups a group) - [+] drills into a block - click a wire, then its x, to delete it.`
    : "Click a block to design it - drag to rearrange - right-click deletes (ungroups a group) - [+] drills into a block - click a wire, then its x, to delete it.";

  return (
    <div className="arch-diagram" data-variant={variant}>
      {(path.length > 0 || crumbs.length > 0) && (
        <div className="arch-breadcrumb">
          <button type="button" className="toggle-raw" onClick={() => setPath([])}>
            {phyType}
          </button>
          {crumbs.map((c, i) => (
            <span key={`${c.id}-${i}`}>
              {" / "}
              <button type="button" className="toggle-raw" onClick={() => setPath(path.slice(0, i + 1))}>
                {c.label}
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="arch-diagram-head">
        <span className="hint">{hint}</span>
        <span>
          <button type="button" className={`toggle-raw ${addMode ? "active" : ""}`} onClick={() => setAddMode((m) => !m)}>
            {addMode ? "Cancel add" : "Add block"}
          </button>{" "}
          <button type="button" className={`toggle-raw ${connectMode ? "active" : ""}`} onClick={toggleConnectMode}>
            {connectMode ? "Cancel connect" : "Add wire"}
          </button>{" "}
          <button type="button" className={`toggle-raw ${groupMode ? "active" : ""}`} onClick={toggleGroupMode}>
            {groupMode ? "Cancel merge" : "Merge blocks"}
          </button>{" "}
          <button type="button" className="toggle-raw" onClick={resetDiagram}>
            Reset diagram
          </button>{" "}
          <button type="button" className="toggle-raw" onClick={savePng}>
            Save PNG
          </button>
        </span>
      </div>
      {addMode && (
        <div className="arch-add-block">
          <input
            type="text"
            placeholder="New block name, e.g. VGA stage 2"
            value={newBlockName}
            onChange={(e) => setNewBlockName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addBlock())}
          />
          <select value={newBlockTopo} onChange={(e) => setNewBlockTopo(e.target.value)}>
            <option value="">{isDigital ? "block type..." : "topology..."}</option>
            {topologyGroups && topologyGroups.length > 0
              ? topologyGroups.map((g) => (
                  <optgroup key={g.group} label={g.group}>
                    {g.options.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </optgroup>
                ))
              : topologyOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
          </select>
          <button type="button" onClick={addBlock} disabled={!newBlockName.trim() || !newBlockTopo}>
            Add
          </button>
          <span className="hint">The new block drops in at the bottom-left - drag it into place, wire it with "Add wire".</span>
        </div>
      )}
      {groupMode && (
        <div className="arch-add-block">
          <input
            type="text"
            placeholder="Group name, e.g. RX analog frontend"
            value={groupName}
            onChange={(e) => setGroupName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), createGroup())}
          />
          <button type="button" onClick={createGroup} disabled={!groupName.trim() || groupSel.length < 2}>
            Create group ({groupSel.length})
          </button>
          <span className="hint">Pick 2+ blocks in the diagram; they move inside the group (drill in with its [+]).</span>
        </div>
      )}
      <div className="arch-diagram-scroll">
        <svg
          ref={svgRef}
          xmlns="http://www.w3.org/2000/svg"
          viewBox={`0 0 ${width} ${height}`}
          style={{
            fontFamily: "system-ui, sans-serif",
            // Fill the available width so the canvas has no dead margin;
            // cap the upscale so a small diagram doesn't balloon.
            width: "100%",
            maxWidth: `${Math.round(width * 1.5)}px`,
            height: "auto",
            display: "block",
            margin: "0 auto",
            touchAction: "none",
          }}
          onClick={() => setSelectedConn(null)}
        >
          <defs>
            <marker id="arch-arrow" markerWidth="9" markerHeight="8" refX="8" refY="4" orient="auto">
              <polygon points="0,0 9,4 0,8" fill="#9aa5b1" />
            </marker>
            <marker id="arch-arrow-sel" markerWidth="9" markerHeight="8" refX="8" refY="4" orient="auto">
              <polygon points="0,0 9,4 0,8" fill="#2563eb" />
            </marker>
          </defs>
          {/* wires first, so blocks draw over them */}
          {conns.map((c) => {
            const { x1, y1, x2, y2 } = wireAnchors(pos[c.from], pos[c.to]);
            const sel = selectedConn === c.key;
            const mx = (x1 + x2) / 2;
            const my = (y1 + y2) / 2;
            return (
              <g key={c.key}>
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke={sel ? "#2563eb" : "#9aa5b1"}
                  strokeWidth={sel ? 2.5 : 1.5}
                  markerEnd={sel ? "url(#arch-arrow-sel)" : "url(#arch-arrow)"}
                />
                {/* fat invisible twin so the thin wire is actually clickable */}
                <line
                  x1={x1}
                  y1={y1}
                  x2={x2}
                  y2={y2}
                  stroke="transparent"
                  strokeWidth="14"
                  style={{ cursor: "pointer" }}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelectedConn(sel ? null : c.key);
                  }}
                />
                {sel && (
                  <g
                    style={{ cursor: "pointer" }}
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteConn(c.key);
                    }}
                  >
                    <circle cx={mx} cy={my} r={9} fill="#dc2626" />
                    <line x1={mx - 4} y1={my - 4} x2={mx + 4} y2={my + 4} stroke="#fff" strokeWidth="2" />
                    <line x1={mx - 4} y1={my + 4} x2={mx + 4} y2={my - 4} stroke="#fff" strokeWidth="2" />
                  </g>
                )}
              </g>
            );
          })}
          {blocks.map((b) => {
            // Boundary anchors: "pad" (AFE view) and "iface" (digital view's
            // AFE/core/host boundary nodes) - drawn rounded/muted, wireable,
            // draggable, never selectable as a design target.
            const isPad = b.kind === "pad" || b.kind === "iface";
            const isGroup = !!b.children;
            const st = blockStates[b.id] || {};
            const status = selectedBlock === b.id && (st.status || "todo") === "todo" ? "selected" : st.status || "todo";
            const style =
              b.kind === "iface"
                ? IFACE_STYLE
                : isPad
                ? PAD_STYLE
                : b.kind === "group"
                ? GROUP_STYLE
                : isFirmware
                ? (selectedBlock === b.id ? FIRMWARE_SELECTED_STYLE : FIRMWARE_BLOCK_STYLE)
                : isDigital
                ? (selectedBlock === b.id ? STATUS_STYLE.selected : DIGITAL_BLOCK_STYLE)
                : STATUS_STYLE[status] || STATUS_STYLE.todo;
            const { x, y } = pos[b.id];
            const cx = x + BLOCK_W / 2;
            // Non-designable blocks (pads, groups, photodiode, digital cal)
            // can't be selected for design, but can still be wired/dragged/
            // deleted/merged/drilled into.
            const disabled = !b.topology && !connectMode && !groupMode;
            const isConnSource = connectSource === b.id;
            const inGroupSel = groupSel.includes(b.id);
            return (
              <g
                key={b.id}
                onPointerDown={(e) => onPointerDown(e, b.id)}
                onPointerMove={onPointerMove}
                onPointerUp={(e) => onPointerUp(e, b)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  deleteBlock(b);
                }}
                style={{
                  cursor: disabled ? "not-allowed" : connectMode || groupMode ? "crosshair" : "grab",
                  opacity: !b.topology && !isPad && !isGroup ? 0.55 : 1,
                }}
              >
                <rect
                  x={x}
                  y={y}
                  width={BLOCK_W}
                  height={BLOCK_H}
                  rx={isPad ? 16 : 8}
                  fill={style.fill}
                  stroke={isConnSource || inGroupSel || selectedBlock === b.id ? accentStroke : style.stroke}
                  strokeWidth={isConnSource || inGroupSel || selectedBlock === b.id ? 3 : 1.5}
                  strokeDasharray={
                    isConnSource || inGroupSel
                      ? "6 4"
                      : b.kind === "group"
                      ? "3 3"
                      : isFirmware && b.topology
                      ? "5 3" // firmware blocks are software - dashed like the overview's SW block
                      : undefined
                  }
                />
                {isFirmware && b.topology && (
                  <g pointerEvents="none">
                    <rect x={x + BLOCK_W - 27} y={y + 5} width={22} height={13} rx={3} fill="#fef3c7" stroke="#d97706" strokeWidth="1" />
                    <text x={x + BLOCK_W - 16} y={y + 15} fontSize="8" fontWeight="700" fill="#92400e" textAnchor="middle">
                      SW
                    </text>
                  </g>
                )}
                <text x={cx} y={y + 24} fontSize="13" fontWeight="600" fill={style.text} textAnchor="middle" pointerEvents="none">
                  {b.short}
                </text>
                <text x={cx} y={y + 42} fontSize="10" fill={style.text} textAnchor="middle" pointerEvents="none">
                  {b.topology
                    ? b.topology
                    : b.kind === "iface"
                    ? "boundary IF"
                    : isPad
                    ? "I/O pad"
                    : b.kind === "group"
                    ? "group (drill in)"
                    : "not designable here"}
                </text>
                {b.topology && !isDigital && (
                  <text x={cx} y={y + BLOCK_H + 14} fontSize="10" fill={style.stroke} textAnchor="middle" pointerEvents="none">
                    {STATUS_STYLE[status].label}
                  </text>
                )}
                {st.detail && (
                  <text x={cx} y={y + BLOCK_H + 27} fontSize="10" fill="#52606d" textAnchor="middle" pointerEvents="none">
                    {truncate(st.detail, 26)}
                  </text>
                )}
                {isGroup && (
                  <g
                    style={{ cursor: "pointer" }}
                    onPointerDown={(e) => e.stopPropagation()}
                    onPointerUp={(e) => e.stopPropagation()}
                    onClick={(e) => {
                      e.stopPropagation();
                      setPath([...path, b.id]);
                    }}
                  >
                    <rect x={x + BLOCK_W - 20} y={y + 4} width={16} height={16} rx={3} fill="#ffffff" stroke={style.stroke} />
                    <line x1={x + BLOCK_W - 16} y1={y + 12} x2={x + BLOCK_W - 8} y2={y + 12} stroke={style.stroke} strokeWidth="1.5" />
                    <line x1={x + BLOCK_W - 12} y1={y + 8} x2={x + BLOCK_W - 12} y2={y + 16} stroke={style.stroke} strokeWidth="1.5" />
                  </g>
                )}
              </g>
            );
          })}
          {usedStatuses.map((s, i) => {
            const style = STATUS_STYLE[s];
            const lx = PAD_MARGIN + i * 150;
            const ly = height - 14;
            return (
              <g key={s}>
                <rect x={lx} y={ly - 9} width={12} height={12} rx={3} fill={style.fill} stroke={style.stroke} />
                <text x={lx + 18} y={ly + 1} fontSize="10" fill="#52606d">
                  {style.label}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
