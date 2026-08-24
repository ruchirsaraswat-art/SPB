import { useMemo, useState } from "react";
import { currentArchitecture } from "./phyArchitectures";

// Generate menu underneath the architecture diagram (user request): produce
// something from the WHOLE displayed diagram (or a chosen subset of blocks):
//   - a composite behavioral model (deliverable "arch_model"): a model per
//     included block plus a top-level module wired per the diagram edges,
//     verified end to end with iverilog. RNM is the default (the only model
//     level applicable to every block type); digital Verilog is offered only
//     when every included block supports it.
//   - a stitched top-level DESIGN schematic (deliverable "arch_stitch"): one
//     xschem schematic instantiating each block's filed library cell symbol,
//     wired per the diagram edges. The user is asked where the top-level
//     stitch goes (library + cell name) and where each individual block is
//     placed from (its filed library cell).
const MODEL_TYPE_OPTIONS = [
  { value: "rnm", label: "RNM (SystemVerilog real) — works for every block type" },
  { value: "verilog", label: "Digital Verilog — clocked/bit-level blocks only" },
];

function sanitizeId(raw) {
  const safe = (raw || "phy").replace(/[^A-Za-z0-9_]/g, "_");
  return /^[A-Za-z_]/.test(safe) ? safe : `b_${safe}`;
}

export default function ArchGenerateMenu({
  phyType,
  archVersion, // bump = the displayed architecture changed; re-read it
  modelsForTopology, // (topology) => {rnm: true|reason, verilog: ..., ...} | null
  libraries = [], // full objects: [{name, cells: [{name, topology, views}]}]
  submitting,
  onSubmit,
}) {
  const [open, setOpen] = useState(false);
  const [output, setOutput] = useState("model"); // "model" | "stitch"
  const [scope, setScope] = useState("all"); // "all" | "selected"
  const [selected, setSelected] = useState({}); // block id -> true
  const [modelType, setModelType] = useState("rnm");
  const [library, setLibrary] = useState(""); // model: optional filing; stitch: REQUIRED destination
  const [topCell, setTopCell] = useState(""); // stitch: top-level cell name
  const [cellMap, setCellMap] = useState({}); // stitch: block id -> "lib/cell"

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const arch = useMemo(() => currentArchitecture(phyType), [phyType, archVersion]);
  const designable = arch.blocks.filter((b) => b.topology);
  const pads = arch.blocks.filter((b) => !b.topology);

  const scopedBlocks =
    scope === "selected" ? designable.filter((b) => selected[b.id]) : designable;

  // --- model-output applicability ------------------------------------------
  function typeProblems(type) {
    const problems = [];
    for (const b of scopedBlocks) {
      const m = modelsForTopology(b.topology);
      if (m && m[type] !== true) problems.push(`${b.short || b.id}: ${m[type]}`);
    }
    return problems;
  }
  const verilogProblems = typeProblems("verilog");
  const effectiveType = modelType === "verilog" && verilogProblems.length ? "rnm" : modelType;

  // --- stitch placement candidates -----------------------------------------
  // For each block: filed cells (any library) matching its topology that have
  // a symbol view (instantiated directly) OR a schematic view (the stitch run
  // generates the missing symbol from it first, and it is filed back into the
  // cell afterwards - user request).
  function candidatesFor(block) {
    const out = [];
    for (const lib of libraries) {
      for (const c of lib.cells || []) {
        if (c.topology !== block.topology) continue;
        const kinds = new Set((c.views || []).map((v) => v.kind));
        if (kinds.has("symbol")) {
          out.push({ ref: `${lib.name}/${c.name}`, needsSymbol: false });
        } else if (kinds.has("schematic")) {
          out.push({ ref: `${lib.name}/${c.name}`, needsSymbol: true });
        }
      }
    }
    return out;
  }
  const stitchable = scopedBlocks.filter((b) => candidatesFor(b).length > 0);
  const unstitchable = scopedBlocks.filter((b) => candidatesFor(b).length === 0);
  const stitchMap = {};
  for (const b of stitchable) {
    stitchMap[b.id] = cellMap[b.id] || candidatesFor(b)[0].ref;
  }
  const defaultTop = `${sanitizeId(phyType)}_top`;

  const includedBlocks = output === "stitch" ? stitchable : scopedBlocks;

  function toggleBlock(id) {
    setSelected((s) => ({ ...s, [id]: !s[id] }));
  }

  function handleGenerate() {
    const common = {
      topology: "custom",
      custom_topology: `${phyType} architecture ${output === "stitch" ? "top-level stitch" : "behavioral model"}`,
      architecture: {
        blocks: arch.blocks.map((b) => ({
          id: b.id,
          label: b.label || b.short || b.id,
          topology: b.topology || null,
        })),
        edges: (arch.edges || []).map((e) => ({ from: e.from, to: e.to })),
      },
      phy_type: phyType,
      vdd_v: 1.8,
      corner: "tt",
      temp_c: 27,
    };
    if (output === "stitch") {
      onSubmit({
        ...common,
        label: `${phyType} top-level stitch (${includedBlocks.length} block${includedBlocks.length === 1 ? "" : "s"} -> ${library}/${topCell || defaultTop})`,
        deliverable: "arch_stitch",
        arch_blocks: includedBlocks.map((b) => b.id),
        arch_cell_map: stitchMap,
        library: library || null,
        cell: topCell || defaultTop,
      });
    } else {
      onSubmit({
        ...common,
        label: `${phyType} architecture model (${scope === "all" ? "entire diagram" : `${includedBlocks.length} block${includedBlocks.length === 1 ? "" : "s"}`})`,
        deliverable: "arch_model",
        arch_blocks: scope === "selected" ? includedBlocks.map((b) => b.id) : null,
        arch_model_type: effectiveType,
        library: library || null,
      });
    }
  }

  const stitchReady =
    output !== "stitch" ||
    (library && includedBlocks.length > 0 && includedBlocks.every((b) => stitchMap[b.id]));

  return (
    <div className="arch-generate-menu">
      <div className="arch-generate-head">
        <strong>Generate from this architecture</strong>
        <button type="button" className="toggle-raw" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide" : "Generate..."}
        </button>
      </div>
      {open && (
        <div className="arch-generate-body">
          <label>
            Output
            <select value={output} onChange={(e) => setOutput(e.target.value)}>
              <option value="model">Behavioral model (per-block models + wired top, verified)</option>
              <option value="stitch">
                Stitched top-level schematic (instantiate filed cells, wire the design)
              </option>
            </select>
          </label>
          <p className="hint">
            {output === "stitch"
              ? "Stitches the diagram together as a design: one top-level xschem schematic placing each block's filed library symbol and wiring them per the drawn connections (netlist + rendered PNG; block internals untouched, no simulation). Blocks whose cell has no symbol yet get one generated from their filed schematic, filed back into the cell."
              : "One behavioral model of this diagram: a model per included block plus a top-level module wired per the drawn connections, verified end to end (iverilog). No transistor-level design."}
          </p>
          <label>
            Scope
            <select value={scope} onChange={(e) => setScope(e.target.value)}>
              <option value="all">Entire block diagram ({designable.length} blocks)</option>
              <option value="selected">Only blocks I pick below</option>
            </select>
          </label>
          {scope === "selected" && (
            <div className="arch-generate-blocks">
              {designable.map((b) => (
                <label key={b.id} className="model-checkbox">
                  <input
                    type="checkbox"
                    checked={!!selected[b.id]}
                    onChange={() => toggleBlock(b.id)}
                  />
                  <span>
                    {b.label || b.id} <code>({b.topology})</code>
                  </span>
                </label>
              ))}
            </div>
          )}
          {pads.length > 0 && (
            <p className="hint">
              Skipped automatically (not modelable):{" "}
              {pads.map((b) => b.short || b.id).join(", ")}
              {output === "stitch" ? " — pad connections become labeled top-level pins." : "."}
            </p>
          )}

          {output === "model" && (
            <>
              <label>
                Model level
                <select value={effectiveType} onChange={(e) => setModelType(e.target.value)}>
                  {MODEL_TYPE_OPTIONS.map((o) => {
                    const problems = o.value === "verilog" ? verilogProblems : [];
                    return (
                      <option key={o.value} value={o.value} disabled={problems.length > 0}>
                        {o.label}
                        {problems.length ? " — not applicable to all included blocks" : ""}
                      </option>
                    );
                  })}
                </select>
              </label>
              {modelType === "verilog" && verilogProblems.length > 0 && (
                <p className="model-disabled-reason">{verilogProblems[0]}</p>
              )}
              <label>
                File into library (optional)
                <select value={library} onChange={(e) => setLibrary(e.target.value)}>
                  <option value="">(don't file — leave in the run directory)</option>
                  {libraries.map((l) => (
                    <option key={l.name} value={l.name}>
                      {l.name}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}

          {output === "stitch" && (
            <>
              <div className="field-row">
                <label>
                  Where should the top-level stitch go? (library)
                  <select value={library} onChange={(e) => setLibrary(e.target.value)}>
                    <option value="">Select a library...</option>
                    {libraries.map((l) => (
                      <option key={l.name} value={l.name}>
                        {l.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Top-level cell name
                  <input
                    type="text"
                    placeholder={defaultTop}
                    value={topCell}
                    onChange={(e) => setTopCell(e.target.value)}
                  />
                </label>
              </div>
              {stitchable.length > 0 && (
                <div className="arch-generate-blocks stitch-placement">
                  <p className="hint">Where should each block be placed from?</p>
                  {stitchable.map((b) => {
                    const cands = candidatesFor(b);
                    const chosen = cands.find((c) => c.ref === stitchMap[b.id]);
                    return (
                      <label key={b.id} className="stitch-placement-row">
                        {b.label || b.id}
                        <select
                          value={stitchMap[b.id]}
                          onChange={(e) => setCellMap((m) => ({ ...m, [b.id]: e.target.value }))}
                        >
                          {cands.map((c) => (
                            <option key={c.ref} value={c.ref}>
                              {c.ref}
                              {c.needsSymbol ? " — symbol will be generated" : ""}
                            </option>
                          ))}
                        </select>
                        {chosen?.needsSymbol && (
                          <span className="hint stitch-symbol-note">
                            no symbol view yet — this run generates it from the cell's schematic
                            and files it back
                          </span>
                        )}
                      </label>
                    );
                  })}
                </div>
              )}
              {unstitchable.length > 0 && (
                <p className="field-warning">
                  Not stitchable yet (no filed library cell with a symbol or schematic view —
                  build the block first):{" "}
                  {unstitchable.map((b) => b.short || b.label || b.id).join(", ")}. These will be
                  left out of the stitch.
                </p>
              )}
            </>
          )}

          <button type="button" onClick={handleGenerate} disabled={submitting || !stitchReady || includedBlocks.length === 0}>
            {submitting
              ? "Working..."
              : output === "stitch"
              ? `Stitch ${includedBlocks.length} block(s) into ${library || "<library>"}/${topCell || defaultTop}`
              : `Generate ${effectiveType === "rnm" ? "RNM" : "Verilog"} model of ${
                  scope === "all" ? "entire diagram" : `${includedBlocks.length} block(s)`
                }`}
          </button>
          {scope === "selected" && scopedBlocks.length === 0 && (
            <p className="hint">Tick at least one block above.</p>
          )}
          {output === "stitch" && !library && (
            <p className="hint">Choose the destination library for the top-level stitch.</p>
          )}
        </div>
      )}
    </div>
  );
}
