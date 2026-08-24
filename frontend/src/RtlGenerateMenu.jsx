import { useState } from "react";
import { validateIfDef } from "./interfaceStore";

// RTL generation UI for the Controller workspace (2026-08-23 rtl-gen spec,
// section c): the per-block "Block RTL" menu shown when a designable digital
// diagram block is selected (catalog-fields mini-form + library picker +
// spec-doc chips + cost-confirmed generate), and the whole-controller
// "Generate controller RTL" launcher with its explicit most-expensive-run
// confirmation card. Field values persist per PHY + block id in localStorage
// `dig-fields-<phy>` ({block_id: {field: value}}) - exactly what the
// request's `block_fields` carries.

export function digFieldsKey(phyType) {
  return `dig-fields-${phyType}`;
}

export function loadDigFields(phyType) {
  try {
    const stored = JSON.parse(localStorage.getItem(digFieldsKey(phyType)));
    if (stored && typeof stored === "object") return stored;
  } catch {
    /* fall through */
  }
  return {};
}

export function saveDigFields(phyType, blockId, values) {
  const all = loadDigFields(phyType);
  all[blockId] = values;
  try {
    localStorage.setItem(digFieldsKey(phyType), JSON.stringify(all));
  } catch {
    /* localStorage unavailable - the values just won't persist */
  }
}

// Required = catalog fields with a `min` defined and no value (text fields
// may stay empty and default per catalog help text - the prompt says so).
export function missingRequiredFields(option, values) {
  return (option?.fields || [])
    .filter((f) => f.min !== undefined)
    .filter((f) => {
      const v = (values || {})[f.name];
      return v === undefined || v === null || v === "";
    })
    .map((f) => f.name);
}

function fieldWarning(f, value) {
  if (value === undefined || value === null || value === "") return null;
  const num = Number(value);
  if (Number.isNaN(num)) return null;
  if (f.warn_above !== undefined && num > f.warn_above) {
    return f.warn_text || `Above ${f.warn_above} is aggressive.`;
  }
  return null;
}

// Same label/unit/help/min/max/warn rendering idea as the spec form, in a
// compact mini-form.
function FieldsMiniForm({ option, values, onChange }) {
  return (
    <div className="rtl-fields-form">
      {(option?.fields || []).map((f) => {
        const value = (values || {})[f.name] ?? "";
        const warn = fieldWarning(f, value);
        const isText = f.type === "text";
        return (
          <label key={f.name} className="rtl-field" title={f.help || ""}>
            <span className="rtl-field-label">
              {f.label}
              {f.unit ? <span className="rtl-field-unit"> ({f.unit})</span> : null}
              {f.min !== undefined && <span className="rtl-field-required"> *</span>}
            </span>
            <input
              type={isText ? "text" : "number"}
              min={f.min}
              max={f.max}
              value={value}
              data-field={f.name}
              onChange={(e) => onChange(f.name, e.target.value)}
            />
            {warn && <span className="field-warning">{warn}</span>}
          </label>
        );
      })}
    </div>
  );
}

function LibraryPicker({ libraries, library, setLibrary, cell, setCell, cellPlaceholder }) {
  return (
    <div className="field-row rtl-library-row">
      <label>
        File into library
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
        Cell override (optional)
        <input type="text" placeholder={cellPlaceholder} value={cell} onChange={(e) => setCell(e.target.value)} />
      </label>
    </div>
  );
}

function SpecDocChips({ specDocs, none }) {
  if (!specDocs?.length) {
    return <p className="hint rtl-spec-note">{none}</p>;
  }
  return (
    <p className="rtl-spec-chips">
      Spec docs passed to the run:{" "}
      {specDocs.map((d) => (
        <span key={d.id} className="spec-chip" title={d.version || ""}>
          {d.title}
        </span>
      ))}
    </p>
  );
}

// --------------------------------------------------------------------------
// Per-block "Block RTL" menu (shown under the diagram on block selection)

export function BlockRtlMenu({
  phyType,
  block, // the selected designable diagram block {id, label, topology}
  option, // its digital-catalog option {value, label, fields}
  libraries,
  specDocs, // docs applying to the controller workspace
  submitting,
  onSubmit, // (partial request) => void; parent adds diagram/interface
}) {
  const [tick, setTick] = useState(0); // re-render after a localStorage write
  const [library, setLibrary] = useState("");
  const [cell, setCell] = useState("");
  const [confirming, setConfirming] = useState(false);

  const values = loadDigFields(phyType)[block.id] || {};
  const missing = missingRequiredFields(option, values);
  const moduleName = block.id.replace(/-/g, "_");

  function handleFieldChange(name, value) {
    saveDigFields(phyType, block.id, { ...values, [name]: value });
    setTick(tick + 1);
  }

  function handleGenerate() {
    setConfirming(false);
    onSubmit({
      scope: "block",
      block_id: block.id,
      library,
      cell: cell || null,
      spec_doc_ids: (specDocs || []).map((d) => d.id),
    });
  }

  return (
    <div className="rtl-block-menu arch-generate-menu">
      <div className="arch-generate-head">
        <strong>
          Block RTL - {block.label || block.id} <code>({block.topology})</code>
        </strong>
      </div>
      <FieldsMiniForm option={option} values={values} onChange={handleFieldChange} />
      <LibraryPicker
        libraries={libraries}
        library={library}
        setLibrary={setLibrary}
        cell={cell}
        setCell={setCell}
        cellPlaceholder={moduleName}
      />
      <SpecDocChips
        specDocs={specDocs}
        none="No spec docs attached - generation is first-principles from the catalog fields + interface."
      />
      {missing.length > 0 && (
        <p className="field-warning rtl-missing-fields">
          Required fields missing: {missing.join(", ")}
        </p>
      )}
      {!confirming ? (
        <button
          type="button"
          className="rtl-generate-btn"
          disabled={submitting || missing.length > 0 || !library}
          onClick={() => setConfirming(true)}
        >
          Generate RTL - {block.label || block.id}
        </button>
      ) : (
        <div className="rtl-confirm-card fw-generate-confirm">
          <span>
            One paid RTL_Coder run for this block: ~$2-5, up to 30 min. The tool re-runs
            iverilog + vvp itself and only reports verified on a real pass.
          </span>
          <button type="button" onClick={handleGenerate}>
            Start run
          </button>
          <button type="button" className="toggle-raw" onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
      {!library && <p className="hint">Choose the destination library.</p>}
    </div>
  );
}

// --------------------------------------------------------------------------
// Whole-controller launcher + confirmation card

export function ControllerRtlLauncher({
  phyType,
  architecture, // {blocks, edges} - the current effective digital diagram
  catalogByValue, // topology value -> catalog option
  libraries,
  specDocs,
  ifDef,
  specAnswer, // "attached" | "no_spec" | null
  submitting,
  onSubmit,
  onOpenSpecDocs,
}) {
  const [open, setOpen] = useState(false);
  const [library, setLibrary] = useState("");
  const [cell, setCell] = useState("");

  const designable = (architecture.blocks || []).filter((b) => b.topology);
  const anchors = (architecture.blocks || []).filter((b) => !b.topology);
  const allFields = loadDigFields(phyType);
  const incomplete = designable
    .map((b) => ({
      block: b,
      missing: missingRequiredFields(catalogByValue[b.topology], allFields[b.id] || {}),
    }))
    .filter((x) => x.missing.length > 0);
  const ifIssues = validateIfDef(ifDef || { signals: [], clock_domains: [], cdc_points: [] });
  const topCell = `${phyType.replace(/-/g, "_")}_controller_top`;

  const blocked = incomplete.length > 0 || ifIssues.allErrors.length > 0 || !library || designable.length === 0;

  function handleGenerate() {
    setOpen(false);
    onSubmit({
      scope: "controller",
      block_id: null,
      library,
      cell: cell || null,
      spec_doc_ids: (specDocs || []).map((d) => d.id),
    });
  }

  return (
    <>
      <button
        type="button"
        className="rtl-controller-btn"
        onClick={() => setOpen((v) => !v)}
        disabled={submitting}
        title="Generate and verify RTL for every designable block plus the stitched controller top"
      >
        Generate controller RTL
      </button>
      {open && (
        <div className="rtl-controller-card">
          <p>
            <strong>{designable.length} designable blocks included:</strong>{" "}
            {designable.map((b) => b.short || b.label || b.id).join(", ")}.
            {anchors.length > 0 && (
              <span className="hint">
                {" "}
                Skipped automatically (interface anchors, not designable):{" "}
                {anchors.map((b) => b.short || b.id).join(", ")}.
              </span>
            )}
          </p>
          {incomplete.length > 0 ? (
            <p className="field-warning">
              Generation blocked - blocks with missing required fields (open each block's
              Block RTL menu to fill them):{" "}
              {incomplete.map((x) => `${x.block.id} (${x.missing.join(", ")})`).join("; ")}
            </p>
          ) : (
            <p className="hint">Per-block field values: complete for all included blocks.</p>
          )}
          {ifIssues.allErrors.length > 0 ? (
            <p className="field-warning">
              Generation blocked - interface hard errors: {ifIssues.allErrors.join("; ")}
            </p>
          ) : (
            <p className="hint">
              Interface: no hard errors
              {ifIssues.allWarnings.length > 0
                ? ` (${ifIssues.allWarnings.length} warning${ifIssues.allWarnings.length === 1 ? "" : "s"} pass into the prompt)`
                : ""}
              .
            </p>
          )}
          {specDocs?.length ? (
            <SpecDocChips specDocs={specDocs} />
          ) : (
            <p className="hint">
              {specAnswer === "no_spec" ? "No spec declared - first principles." : "No spec docs attached."}{" "}
              <button type="button" className="toggle-raw" onClick={onOpenSpecDocs}>
                open spec-doc intake
              </button>
            </p>
          )}
          <LibraryPicker
            libraries={libraries}
            library={library}
            setLibrary={setLibrary}
            cell={cell}
            setCell={setCell}
            cellPlaceholder={topCell}
          />
          {/* Estimate recalibrated 2026-08-24 from the first real block run
              ($4.21 / 8.2 min for elastic_fifo) pro-rata - see the rtl-gen
              spec's calibration appendix. */}
          <p className="rtl-cost-line">
            This is the most expensive run type in this tool: estimated $25-55 and 60-120
            minutes for {designable.length} blocks. It generates and verifies every block
            plus the stitched top.
          </p>
          <div className="rtl-controller-actions">
            <button type="button" disabled={blocked || submitting} onClick={handleGenerate}>
              Generate {designable.length} blocks + top into {library || "<library>"}/{cell || topCell}
            </button>
            <button type="button" className="toggle-raw" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
          {!library && <p className="hint">Choose the destination library to enable generation.</p>}
        </div>
      )}
    </>
  );
}
