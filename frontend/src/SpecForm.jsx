import { useEffect, useState } from "react";
import { createLibrary, getTopologies, getToolchain, listLibraries } from "./api";
import PhyArchitectureSelector from "./PhyArchitectureSelector";
import ArchGenerateMenu from "./ArchGenerateMenu";

const NEW_LIBRARY = "__new__";

const CURRENT_MIRROR = "nmos_current_mirror";

// Behavioral-model types (cycle 2). Which of these are offered per topology
// comes from the backend's applicability matrix (per-option "models" key on
// /api/topologies); a disabled checkbox's tooltip is the matrix's reason
// string (e.g. "clocked blocks can't be expressed in the OpenVAF subset").
const MODEL_TYPES = [
  { value: "veriloga", label: "Verilog-A", detail: "continuous-time, runs inside ngspice (OpenVAF/OSDI)" },
  { value: "verilog", label: "Digital Verilog", detail: "event-driven bit-level model for RTL sims" },
  { value: "rnm", label: "RNM (SystemVerilog real)", detail: "real-number model for fast full-link sims" },
];

// Single-deliverable run modes (Generate dropdown). "full" is the original
// design+simulate(+models) flow; the model-type options are subject to the
// same per-topology applicability matrix as the checkboxes and render
// disabled (with the matrix's reason) when inapplicable.
const DELIVERABLE_OPTIONS = [
  { value: "full", label: "Full flow (design + simulate + models)" },
  { value: "schematic_only", label: "Schematic only (draw + netlist + PNG, no sim suite)" },
  { value: "symbol", label: "Symbol only (xschem .sym)" },
  { value: "veriloga", label: "Verilog-A model only", model: "veriloga" },
  { value: "verilog", label: "Digital Verilog model only", model: "verilog" },
  { value: "rnm", label: "RNM model only (SystemVerilog real)", model: "rnm" },
];

const MODEL_DELIVERABLES = ["veriloga", "verilog", "rnm"];

const SUBMIT_LABELS = {
  schematic_only: "Generate schematic",
  symbol: "Generate symbol",
  veriloga: "Generate Verilog-A model",
  verilog: "Generate Verilog model",
  rnm: "Generate RNM model",
};

// Which locally-probed tool verifies each model type - used for the inline
// "will be generated but unverified" warning when that tool is missing.
function modelToolWarning(type, toolchain) {
  if (!toolchain) return null;
  if (type === "veriloga") {
    return toolchain.openvaf?.available
      ? null
      : "Will be generated but NOT verified - openvaf is not installed on the backend machine.";
  }
  return toolchain.iverilog?.available
    ? null
    : "Will be generated but NOT verified - iverilog (Icarus Verilog) is not installed on the backend machine (needs `sudo apt install iverilog`).";
}

// Help text for the fixed (non-topology-specific) spec fields; per-topology
// field help comes from the backend's TOPOLOGY_FIELDS via /api/topologies.
const FIXED_HELP = {
  power_mw: "Total power budget for this block. Circuit_Builder reports the simulated supply current x VDD against it.",
  area_um2: "Rough silicon area budget. Treated as a soft constraint when picking topology/device sizes.",
  vdd_v: "Supply voltage. sky130 nfet/pfet_01v8 devices are rated 1.8 V nominal, usable up to ~1.98 V (higher needs the 5V device family, which this flow doesn't set up) - lower supplies cost headroom (stacked cascodes get hard).",
  notes: "Free-text extras the structured fields don't cover: signaling (NRZ/PAM4), channel description, single-ended vs differential, anything else the designer should know.",
  temp_c: "Simulation temperature (.temp in the testbench; ngspice defaults to 27 C). Worst-case speed is really ss + 125 C + low VDD - and a bandgap's tempco is only measurable with a temperature sweep.",
  corner: "sky130 process corner the simulation runs at: tt = typical, ff = fast, ss = slow (worst for speed); sf/fs (skewed N vs P) are worst for offset/symmetry specs like comparator threshold and duty cycle.",
  testbench_style: "SPICE = Circuit_Builder hand-writes the testbench netlist. xschem = it draws the testbench as a schematic (openable in the GUI), then netlists it. Both run in ngspice as <circuit>_tb.spice.",
};

// Soft sky130 feasibility warning for one topology field - thresholds/text
// are schema-driven (warn_above/warn_below/warn_text from the backend's
// TOPOLOGY_FIELDS via /api/topologies). Non-blocking by design: the value
// still submits, the user has just been told it's beyond what sky130 has
// demonstrably done. Hard physical bounds are separate (min/max attributes
// on the input, which block submit, plus backend validators).
function extraFieldWarning(f, raw) {
  if (f.type === "text" || raw === undefined || raw === null || raw === "") return null;
  const v = Number(raw);
  if (Number.isNaN(v)) return null;
  if (f.warn_above != null && v > f.warn_above) {
    return f.warn_text || `Above ${f.warn_above}${f.unit ? " " + f.unit : ""} is beyond sky130 feasibility.`;
  }
  if (f.warn_below != null && v < f.warn_below) {
    return f.warn_text || `Below ${f.warn_below}${f.unit ? " " + f.unit : ""} is questionable.`;
  }
  return null;
}

// Wraps one form field; shows its explanatory note while the field is
// hovered or clicked (focused), and hides it when the cursor moves away.
function FieldWithNote({ name, help, activeNote, setActiveNote, children }) {
  if (!help) return children;
  return (
    <div
      className="field-with-note"
      onMouseEnter={() => setActiveNote(name)}
      onMouseLeave={() => setActiveNote((n) => (n === name ? null : n))}
      onFocusCapture={() => setActiveNote(name)}
      onBlurCapture={() => setActiveNote((n) => (n === name ? null : n))}
    >
      {children}
      {activeNote === name && <p className="field-note">{help}</p>}
    </div>
  );
}

const DEFAULTS = {
  label: "",
  phy_type: "ser-des",
  selected_block: null,
  // No topology pre-selected (user request, cycle 2): the starting screen
  // keeps the spec section empty until the user clicks a block in the
  // architecture diagram (or picks a topology from the dropdown) - the form
  // should not open pre-loaded with the current-mirror spec.
  topology: null,
  custom_topology: "",
  // current-mirror-specific fields
  iref_ua: 10,
  ratio_out: 1,
  ratio_ref: 1,
  w_ref_um: 2.0,
  l_ref_um: 0.5,
  // topology-specific structured fields (schema comes from GET /api/topologies
  // per option, backed by backend/topologies.py TOPOLOGY_FIELDS), keyed by
  // field name - e.g. for ctle: dc_gain_db, peaking_db, data_rate_gbps, ...
  extra_fields: {},
  // budget/context fields shared by every non-current-mirror topology
  power_mw: "",
  area_um2: "",
  notes: "",
  // common to every topology
  vdd_v: 1.8,
  temp_c: 27,
  corner: "tt",
  // how Circuit_Builder should author the testbench: a hand-written SPICE
  // file, or an xschem schematic (netlisted headlessly before simulation)
  testbench_style: "spice",
  // behavioral-model types to generate alongside the transistor-level
  // design (opt-in: model generation/verification meaningfully lengthens a
  // Circuit_Builder run, so nothing is pre-checked)
  models: [],
  // what this run produces: "full" (default flow) or exactly one deliverable
  // (schematic_only / symbol / veriloga / verilog / rnm)
  deliverable: "full",
  // Design library (library/cell/view structure) the built block is filed into (cells and
  // their views land in libraries/<library>/<cell>/, browsable from
  // xschem). The form requires a choice; existing libraries are offered
  // and a new one can be created inline.
  library: "",
  // Existing cell of the chosen library to work on (its views get
  // added/refreshed by the run); null = the run creates/names a new cell.
  cell: null,
};

// `phyType` (2026-08-23): the PHY Type / Architecture pull-down moved up
// into the top-level overview panel (App owns the choice, incl. the saved-
// architecture "custom:" loads), so the form now receives the active
// phy_type as a controlled prop and syncs its spec to it. `archVersion`
// (arch-chat feature) bumps whenever the displayed architecture was edited
// from outside the diagram (chat patch apply/undo, custom load) so the
// diagram re-reads.
export default function SpecForm({
  onSubmit,
  submitting,
  onArchChange,
  blockStates,
  settings,
  archVersion,
  phyType = DEFAULTS.phy_type,
}) {
  const [spec, setSpec] = useState(DEFAULTS);
  // Follow the overview panel's PHY selector: a PHY switch invalidates the
  // currently highlighted block (it belongs to the old architecture).
  useEffect(() => {
    setSpec((s) => (s.phy_type === phyType ? s : { ...s, phy_type: phyType, selected_block: null }));
  }, [phyType]);
  const [topoGroups, setTopoGroups] = useState([]);
  const [customValue, setCustomValue] = useState("custom");
  const [customModels, setCustomModels] = useState(null); // applicability map for the free-text "custom" topology
  const [topoError, setTopoError] = useState(null);
  // Model-verification tool availability on the backend machine (openvaf /
  // iverilog) - null until fetched; failure to fetch just means no inline
  // "unverified" warnings, not a broken form.
  const [toolchain, setToolchain] = useState(null);
  // Design libraries for the library picker: full objects ({name, cells:
  // [{name, topology, views}]}) so the cell picker can list a chosen
  // library's cells; plus inline new-library creation state.
  const [libraries, setLibraries] = useState([]);
  const [libChoice, setLibChoice] = useState(""); // "" | existing name | NEW_LIBRARY
  const [newLibName, setNewLibName] = useState("");
  const [libBusy, setLibBusy] = useState(false);
  const [libError, setLibError] = useState(null);
  // Which field's explanatory note is showing (set on hover/click of a
  // field, cleared when the cursor moves away).
  const [activeNote, setActiveNote] = useState(null);

  useEffect(() => {
    getTopologies()
      .then((data) => {
        setTopoGroups(data.groups || []);
        setCustomValue(data.custom_value || "custom");
        setCustomModels(data.custom_models || null);
      })
      .catch((e) => setTopoError(String(e)));
    getToolchain()
      .then(setToolchain)
      .catch(() => setToolchain(null));
    listLibraries()
      .then((data) => setLibraries(data.libraries || []))
      .catch(() => setLibraries([]));
  }, []);

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
      setSpec((s) => ({ ...s, library: name, cell: null }));
      setNewLibName("");
    } catch (e) {
      setLibError(e instanceof Error ? e.message : String(e));
    } finally {
      setLibBusy(false);
    }
  }

  // Keep the parent's complete-architecture diagram and schematic sub-window
  // in sync with what's being worked on in the form: PHY type, which
  // sub-block is highlighted, and the topology it maps to (block clicks set
  // topology too, so the panel follows both selection paths).
  useEffect(() => {
    const option = topoGroups.flatMap((g) => g.options).find((o) => o.value === spec.topology);
    const label =
      spec.topology === CURRENT_MIRROR
        ? "NMOS current mirror (2-transistor)"
        : spec.topology === customValue
        ? spec.custom_topology || "Custom topology"
        : option?.label || spec.topology;
    onArchChange?.({
      phy_type: spec.phy_type,
      selected_block: spec.selected_block,
      topology: spec.topology,
      topology_label: spec.topology ? label : null,
    });
  }, [spec.phy_type, spec.selected_block, spec.topology, spec.custom_topology, topoGroups, customValue, onArchChange]);

  function update(field, value) {
    setSpec((s) => ({ ...s, [field]: value }));
  }

  // Applicability map {type: true | "<reason>"} for an arbitrary topology
  // value (the free-text custom topology has no option entry - its map is
  // served separately). Null until /api/topologies has loaded.
  function modelsForTopology(value) {
    if (value === customValue) return customModels;
    return topoGroups.flatMap((g) => g.options).find((o) => o.value === value)?.models || null;
  }

  // A model-only deliverable valid for the old topology may be inapplicable
  // to the new one (which the backend would 422) - fall back to "full" then.
  function nextDeliverable(current, topology) {
    if (!MODEL_DELIVERABLES.includes(current)) return current;
    const m = modelsForTopology(topology);
    return m && m[current] === true ? current : "full";
  }

  // Changing topology invalidates any topology-specific field values already
  // entered (a CTLE's peaking_db means nothing to a PLL), so clear them -
  // and clear the model checkboxes too (a type valid for the old topology
  // may be inapplicable to the new one, which the backend would 422).
  function updateTopology(topology) {
    setSpec((s) => ({
      ...s,
      topology,
      extra_fields: {},
      models: [],
      deliverable: nextDeliverable(s.deliverable, topology),
      // A manually-changed topology invalidates a targeted existing cell
      // (its views are for the previous topology) - back to "new cell".
      cell: s.cell && cellFor(s.library, s.cell)?.topology !== topology ? null : s.cell,
    }));
  }

  // The chosen library's cell list / one cell record, from the fetched
  // library objects (empty until /api/libraries loads).
  function cellsFor(library) {
    return libraries.find((l) => l.name === library)?.cells || [];
  }
  function cellFor(library, cell) {
    return cellsFor(library).find((c) => c.name === cell) || null;
  }

  // Picking an existing cell targets the run at it (views added/refreshed
  // there) and pulls the form's topology from the cell's provenance so the
  // spec matches what the cell is.
  function handleCellChoice(name) {
    if (!name) {
      setSpec((s) => ({ ...s, cell: null }));
      return;
    }
    const cellRec = cellFor(spec.library, name);
    setSpec((s) => {
      const topology = cellRec?.topology || s.topology;
      const topologyChanged = topology !== s.topology;
      return {
        ...s,
        cell: name,
        topology,
        extra_fields: topologyChanged ? {} : s.extra_fields,
        models: topologyChanged ? [] : s.models,
        deliverable: nextDeliverable(s.deliverable, topology),
      };
    });
  }

  function toggleModel(type) {
    setSpec((s) => ({
      ...s,
      models: s.models.includes(type) ? s.models.filter((m) => m !== type) : [...s.models, type],
    }));
  }

  function updateExtraField(name, value) {
    setSpec((s) => ({ ...s, extra_fields: { ...s.extra_fields, [name]: value } }));
  }

  // Receives the full block object (not just an id) so user-added blocks -
  // which aren't in the static PHY definition - resolve their topology too.
  function handleBlockSelected(block) {
    if (block?.topology) {
      setSpec((s) => ({
        ...s,
        selected_block: block.id,
        topology: block.topology,
        extra_fields: {},
        models: [],
        deliverable: nextDeliverable(s.deliverable, block.topology),
        cell: s.cell && cellFor(s.library, s.cell)?.topology !== block.topology ? null : s.cell,
      }));
    }
  }

  const isCurrentMirror = spec.topology === CURRENT_MIRROR;
  const isCustom = spec.topology === customValue;
  const hasBlockSelected = spec.selected_block !== null;
  // Nothing chosen yet (fresh start screen): show only the PHY picker /
  // architecture diagram / topology dropdown, keep every spec field hidden.
  const hasTopology = spec.topology !== null && spec.topology !== "";

  // Per-topology field schema, as served by GET /api/topologies (each option
  // carries a "fields" list - see backend/topologies.py TOPOLOGY_FIELDS).
  const currentOption = topoGroups.flatMap((g) => g.options).find((o) => o.value === spec.topology);
  const currentFields = currentOption?.fields || [];
  // Behavioral-model applicability for the selected topology: {type: true |
  // "<reason disabled>"}. The free-text custom topology has no option entry;
  // its map is served separately as custom_models. Null until /api/topologies
  // has loaded - the checkbox group hides itself then.
  const currentModels = isCustom ? customModels : currentOption?.models || null;

  // Non-blocking cross-field sanity warnings (the per-field warn_above/
  // warn_below ones are schema-driven via extraFieldWarning above).
  const vddNum = Number(spec.vdd_v);
  const vddWarning =
    vddNum > 0 && vddNum < 1.2
      ? "Low supply for sky130: pfet_01v8 |Vt| is ~0.6-0.9 V, so expect lvt devices and reduced stacking headroom."
      : null;

  // Derived mirror-device width must also satisfy sky130's 0.42 um minimum
  // gate width - the backend rejects it, this just says so before submit.
  const wMirrorUm =
    (Number(spec.w_ref_um) * Number(spec.ratio_out)) / Number(spec.ratio_ref || 1);
  const mirrorWidthWarning =
    isCurrentMirror && wMirrorUm > 0 && wMirrorUm < 0.42
      ? `Derived mirror width ${wMirrorUm.toFixed(3)} um (W_ref x ratio) is below sky130's 0.42 um minimum gate width - widen W or change the ratio.`
      : null;

  // Serializer/deserializer: the word clock is by definition
  // serial rate / width, so flag an inconsistent hand-entered triple.
  const parallelClockWarning = (() => {
    const rate = Number(spec.extra_fields.serial_data_rate_gbps);
    const width = Number(spec.extra_fields.parallel_width_bits);
    const clk = Number(spec.extra_fields.parallel_clock_freq_mhz);
    if (!rate || !width || !clk) return null;
    const derived = (rate * 1000) / width;
    if (Math.abs(clk - derived) / derived <= 0.01) return null;
    return `Inconsistent: ${rate} Gb/s over ${width} bits gives a ${derived.toFixed(2)} MHz word clock, not ${clk} MHz (parallel clock = serial rate / width).`;
  })();

  function handleSubmit(e) {
    e.preventDefault();
    const payload = {
      label: spec.label || null,
      topology: spec.topology,
      custom_topology: isCustom ? spec.custom_topology || null : null,
      phy_type: spec.phy_type,
      selected_block: spec.selected_block,
      vdd_v: Number(spec.vdd_v),
      temp_c: Number(spec.temp_c),
      corner: spec.corner,
      testbench_style: spec.testbench_style,
      // Single-deliverable mode; the model checkboxes only apply to "full"
      // runs (the form hides them otherwise, and the backend rejects the
      // contradictory combination).
      deliverable: spec.deliverable,
      // Behavioral models to generate (build-time only - App.jsx's research
      // subset deliberately omits this; Circuit_Researcher compares
      // topologies, it doesn't write models).
      models: spec.deliverable === "full" && spec.models.length ? spec.models : null,
      // Design library the successful build is filed into (required by the
      // form; the submit button stays disabled until one is chosen), plus
      // the existing cell to file into when the user targeted one.
      library: spec.library || null,
      cell: spec.cell || null,
    };
    if (isCurrentMirror) {
      payload.iref_ua = Number(spec.iref_ua);
      payload.ratio_out = Number(spec.ratio_out);
      payload.ratio_ref = Number(spec.ratio_ref);
      payload.w_ref_um = Number(spec.w_ref_um);
      payload.l_ref_um = Number(spec.l_ref_um);
    } else {
      const extra = {};
      for (const f of currentFields) {
        const v = spec.extra_fields[f.name];
        if (v === undefined || v === "") continue;
        extra[f.name] = f.type === "text" ? v : Number(v);
      }
      payload.extra_fields = Object.keys(extra).length ? extra : null;
      payload.power_mw = spec.power_mw === "" ? null : Number(spec.power_mw);
      payload.area_um2 = spec.area_um2 === "" ? null : Number(spec.area_um2);
      payload.notes = spec.notes || null;
    }
    onSubmit(payload);
  }

  return (
    <form className="spec-form" onSubmit={handleSubmit}>
      <h2>Circuit Spec</h2>

      {/* The PHY Type / Architecture pull-down lives in the top-level
          "PHY architecture" overview panel now (App.jsx / PhyTypeSelect). */}
      <PhyArchitectureSelector
        phy_type={spec.phy_type}
        selectedBlock={spec.selected_block}
        blockStates={blockStates}
        topologyOptions={topoGroups
          .filter((g) => g.group !== "Built-in (detailed sizing flow)")
          .flatMap((g) => g.options)
          .map((o) => ({ value: o.value, label: o.label }))}
        onSelect={handleBlockSelected}
        archVersion={archVersion}
      />

      {/* Generate menu underneath the architecture pane (user request):
          behavioral model of the entire diagram or of chosen blocks. */}
      <ArchGenerateMenu
        phyType={spec.phy_type}
        archVersion={archVersion}
        modelsForTopology={modelsForTopology}
        libraries={libraries}
        submitting={submitting}
        onSubmit={onSubmit}
      />

      <label>
        Run label (optional)
        <input
          type="text"
          placeholder="e.g. 2:1 mirror, 1.8V"
          value={spec.label}
          onChange={(e) => update("label", e.target.value)}
        />
      </label>

      {hasBlockSelected && (
        <p className="hint highlight">
          <strong>Selected PHY block:</strong> {spec.selected_block}
        </p>
      )}

      {!hasTopology && (
        <p className="hint highlight">
          Click a block in the architecture diagram above (or pick a topology below) to enter its
          spec - the form stays empty until then.
        </p>
      )}

      <label>
        Topology
        <select value={spec.topology ?? ""} onChange={(e) => updateTopology(e.target.value)}>
          <option value="" disabled>
            Select a topology...
          </option>
          <optgroup label="Built-in (detailed sizing flow)">
            <option value={CURRENT_MIRROR}>NMOS current mirror (2-transistor)</option>
          </optgroup>
          {topoGroups
            .filter((g) => g.group !== "Built-in (detailed sizing flow)")
            .map((g) => (
              <optgroup key={g.group} label={g.group}>
                {g.options.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </optgroup>
            ))}
          <option value={customValue}>Other / custom...</option>
        </select>
      </label>
      {topoError && (
        <p className="hint">
          Could not load the full topology list from the backend ({topoError}) - only the built-in current
          mirror is available until the backend is reachable.
        </p>
      )}

      {isCustom && (
        <label>
          Custom topology name
          <input
            type="text"
            placeholder="e.g. inductive-peaked CTLE"
            required
            value={spec.custom_topology}
            onChange={(e) => update("custom_topology", e.target.value)}
          />
        </label>
      )}

      {hasTopology && !isCurrentMirror && (
        <p className="hint">
          {isCustom
            ? "For a custom block, this tool has Circuit_Researcher compare a couple of candidate architectures first, then hands your choice to Circuit_Builder."
            : "This topology category doesn't have a fixed internal architecture in this tool - the next step lets Circuit_Researcher compare a couple of candidate architectures for it and you pick one before Circuit_Builder builds it."}
        </p>
      )}

      {hasTopology && (
        <div className="library-picker">
          <label>
            Design library (the built cell and its views are filed here)
            <select
              value={libChoice}
              onChange={(e) => {
                const v = e.target.value;
                setLibChoice(v);
                setLibError(null);
                setSpec((s) => ({ ...s, library: v === NEW_LIBRARY ? "" : v, cell: null }));
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
          {spec.library && libChoice !== NEW_LIBRARY && (
            <div className="cell-picker">
              <label>
                Cell (work on an existing cell's views, or generate a new one)
                <select value={spec.cell ?? ""} onChange={(e) => handleCellChoice(e.target.value)}>
                  <option value="">+ Generate a new cell</option>
                  {cellsFor(spec.library).map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name}
                      {c.topology ? ` — ${c.topology}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {spec.cell && cellFor(spec.library, spec.cell) && (
                <p className="hint cell-views-hint">
                  Existing views of <code>{spec.cell}</code>:{" "}
                  {[...new Set(
                    (cellFor(spec.library, spec.cell).views || [])
                      .map((v) => v.kind)
                      .filter((k) => !["provenance", "log", "other"].includes(k))
                  )].join(", ") || "none"}
                  . The Generate mode below adds or refreshes this cell's views (a full run
                  refreshes schematic/netlist/measurements; symbol/model modes touch only
                  their own view).
                </p>
              )}
            </div>
          )}
          {libChoice === NEW_LIBRARY && (
            <div className="field-row new-library-row">
              <label>
                New library name
                <input
                  type="text"
                  placeholder="e.g. serdes_rx_v1"
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
          {libError && <p className="field-warning">{libError}</p>}
          <p className="hint">
            On a successful build the schematic, symbol, netlist, testbench, rendered image,
            behavioral models and measurements are copied into{" "}
            <code>
              {settings?.derived?.libraries || "libraries"}/{spec.library || "<library>"}/&lt;cell&gt;/
            </code>{" "}
            - a lib/cell/view tree that xschem browses directly (it's on every run's
            XSCHEM_LIBRARY_PATH).
            {settings ? " The libraries root comes from the working directory in Settings (gear, top right)." : ""}
          </p>
        </div>
      )}

      {hasTopology && isCurrentMirror && (
        <>
          <div className="field-row">
            <label>
              Reference current I<sub>ref</sub> (&micro;A)
              <input
                type="number"
                step="any"
                min="0.01"
                required
                value={spec.iref_ua}
                onChange={(e) => update("iref_ua", e.target.value)}
              />
            </label>

            <label>
              Supply voltage V<sub>DD</sub> (V)
              <input
                type="number"
                step="any"
                min="0.1"
                max="1.98"
                required
                value={spec.vdd_v}
                onChange={(e) => update("vdd_v", e.target.value)}
              />
            </label>
          </div>
          {vddWarning && <p className="field-warning">{vddWarning}</p>}

          <div className="field-row">
            <label>
              Mirror ratio - output side
              <input
                type="number"
                step="any"
                min="0.01"
                required
                value={spec.ratio_out}
                onChange={(e) => update("ratio_out", e.target.value)}
              />
            </label>
            <span className="ratio-colon">:</span>
            <label>
              Mirror ratio - reference side
              <input
                type="number"
                step="any"
                min="0.01"
                required
                value={spec.ratio_ref}
                onChange={(e) => update("ratio_ref", e.target.value)}
              />
            </label>
          </div>
          <p className="hint">
            e.g. 1:1, 2:1, or N:1 (output:reference). Target output current ={" "}
            {(
              (Number(spec.iref_ua) * Number(spec.ratio_out)) /
              Number(spec.ratio_ref || 1)
            ).toFixed(3)}{" "}
            &micro;A
          </p>

          <div className="field-row">
            <label>
              Reference device width W (&micro;m)
              <input
                type="number"
                step="any"
                min="0.42"
                required
                value={spec.w_ref_um}
                onChange={(e) => update("w_ref_um", e.target.value)}
              />
            </label>
            <label>
              Reference device length L (&micro;m)
              <input
                type="number"
                step="any"
                min="0.15"
                required
                value={spec.l_ref_um}
                onChange={(e) => update("l_ref_um", e.target.value)}
              />
            </label>
          </div>
          {mirrorWidthWarning && <p className="field-warning">{mirrorWidthWarning}</p>}
          <p className="hint">
            v1 sizing model: you give the reference device's W/L directly; the
            mirror device's W is derived by scaling by the ratio (same L).
            Circuit_Builder verifies the resulting current via simulation rather
            than solving for a target compliance voltage. W minimum is 0.42
            &micro;m - sky130's custom-layout minimum gate width (0.15 &micro;m
            applies to gate length only).
          </p>
        </>
      )}
      {hasTopology && !isCurrentMirror && (
        <>
          {currentFields.length > 0 && (
            <>
              <h3 className="spec-section-title">Block-specific targets (all optional)</h3>
              {currentFields.map((f) => {
                const warning = extraFieldWarning(f, spec.extra_fields[f.name]);
                return (
                  <FieldWithNote key={f.name} name={f.name} help={f.help} activeNote={activeNote} setActiveNote={setActiveNote}>
                    <label>
                      {f.label}
                      {f.unit ? ` (${f.unit})` : ""}
                      <input
                        type={f.type === "text" ? "text" : "number"}
                        step={f.type === "text" ? undefined : "any"}
                        min={f.type === "text" ? undefined : f.min}
                        max={f.type === "text" ? undefined : f.max}
                        value={spec.extra_fields[f.name] ?? ""}
                        onChange={(e) => updateExtraField(f.name, e.target.value)}
                      />
                    </label>
                    {warning && <p className="field-warning">{warning}</p>}
                  </FieldWithNote>
                );
              })}
              {parallelClockWarning && <p className="field-warning">{parallelClockWarning}</p>}
            </>
          )}
          <div className="field-row">
            <label>
              Power budget (mW, optional)
              <input
                type="number"
                step="any"
                min="0"
                value={spec.power_mw}
                onChange={(e) => update("power_mw", e.target.value)}
              />
            </label>
            <label>
              Area budget (&micro;m&sup2;, optional)
              <input
                type="number"
                step="any"
                min="0"
                value={spec.area_um2}
                onChange={(e) => update("area_um2", e.target.value)}
              />
            </label>
          </div>
          <label>
            Supply voltage V<sub>DD</sub> (V)
            <input
              type="number"
              step="any"
              min="0.1"
              max="1.98"
              required
              value={spec.vdd_v}
              onChange={(e) => update("vdd_v", e.target.value)}
            />
          </label>
          {vddWarning && <p className="field-warning">{vddWarning}</p>}
          <label>
            Other spec details / notes (optional)
            <input
              type="text"
              placeholder="e.g. 2.5 Gb/s NRZ, single-ended input"
              value={spec.notes}
              onChange={(e) => update("notes", e.target.value)}
            />
          </label>
          <p className="hint">
            Leave any of these blank if you don't have a hard requirement -
            Circuit_Researcher/Circuit_Builder will use their own judgment and
            say what they assumed.
          </p>
        </>
      )}

      {hasTopology && (
        <>
      <div className="field-row">
        <FieldWithNote name="corner" help={FIXED_HELP.corner} activeNote={activeNote} setActiveNote={setActiveNote}>
          <label>
            Process corner
            <select value={spec.corner} onChange={(e) => update("corner", e.target.value)}>
              <option value="tt">tt (typical)</option>
              <option value="ff">ff (fast)</option>
              <option value="ss">ss (slow - worst for speed)</option>
              <option value="sf">sf (slow N / fast P - skewed)</option>
              <option value="fs">fs (fast N / slow P - skewed)</option>
            </select>
          </label>
        </FieldWithNote>
        <FieldWithNote name="temp_c" help={FIXED_HELP.temp_c} activeNote={activeNote} setActiveNote={setActiveNote}>
          <label>
            Temperature (&deg;C)
            <input
              type="number"
              step="any"
              min="-40"
              max="150"
              required
              value={spec.temp_c}
              onChange={(e) => update("temp_c", e.target.value)}
            />
          </label>
        </FieldWithNote>
      </div>

      <label>
        Testbench style
        <select value={spec.testbench_style} onChange={(e) => update("testbench_style", e.target.value)}>
          <option value="spice">SPICE file (hand-written netlist testbench)</option>
          <option value="xschem">xschem schematic (netlisted before simulation)</option>
        </select>
      </label>
      <p className="hint">
        Either way the testbench is named <code>&lt;circuit&gt;_tb</code> and runs in ngspice; the xschem
        option also gives you a testbench schematic you can open and edit in the GUI.
      </p>

      {currentModels && spec.deliverable === "full" && (
        <fieldset className="models-group">
          <legend>Behavioral models (optional)</legend>
          <p className="hint">
            Ask Circuit_Builder to also write simulation models of this block at the checked
            abstraction levels, verified against the spec with self-checking testbenches where the
            tools allow. Adds noticeable run time.
          </p>
          {MODEL_TYPES.map((mt) => {
            const applicable = currentModels[mt.value] === true;
            const reason = applicable ? null : currentModels[mt.value];
            const checked = spec.models.includes(mt.value);
            const toolWarning = applicable && checked ? modelToolWarning(mt.value, toolchain) : null;
            return (
              <div key={mt.value} className="model-choice" title={reason || undefined}>
                <label className={`model-checkbox ${applicable ? "" : "disabled"}`}>
                  <input
                    type="checkbox"
                    disabled={!applicable}
                    checked={checked}
                    onChange={() => toggleModel(mt.value)}
                  />
                  <span>
                    <strong>{mt.label}</strong> — {mt.detail}
                  </span>
                </label>
                {reason && <p className="model-disabled-reason">{reason}</p>}
                {toolWarning && <p className="field-warning">{toolWarning}</p>}
              </div>
            );
          })}
        </fieldset>
      )}

      <label className="generate-mode">
        Generate
        <select
          value={spec.deliverable}
          onChange={(e) => update("deliverable", e.target.value)}
        >
          {DELIVERABLE_OPTIONS.map((opt) => {
            const applicable = !opt.model || (currentModels && currentModels[opt.model] === true);
            const reason = opt.model && currentModels && currentModels[opt.model] !== true
              ? currentModels[opt.model]
              : null;
            return (
              <option key={opt.value} value={opt.value} disabled={!applicable} title={reason || undefined}>
                {opt.label}
                {!applicable ? " — not applicable to this topology" : ""}
              </option>
            );
          })}
        </select>
      </label>
      {(() => {
        // Ineligible model options render disabled in the dropdown above;
        // their applicability reasons (the same strings the checkboxes use)
        // are listed compactly here so a disabled option is explained even
        // where <option title> tooltips don't show.
        const disabledOptions = DELIVERABLE_OPTIONS.filter(
          (o) => o.model && currentModels && currentModels[o.model] !== true
        );
        if (spec.deliverable === "full") {
          return disabledOptions.length > 0 ? (
            <p className="model-disabled-reason generate-disabled-reasons">
              {disabledOptions
                .map((o) => `${o.label.replace(" only", "")} is disabled for this topology`)
                .join("; ")}{" "}
              — reasons under the model checkboxes below.
            </p>
          ) : null;
        }
        const toolWarning = MODEL_DELIVERABLES.includes(spec.deliverable)
          ? modelToolWarning(spec.deliverable, toolchain)
          : null;
        return (
          <>
            <p className="hint">
              {spec.deliverable === "schematic_only"
                ? "Schematic-only run: designs the circuit and produces the xschem schematic, netlist and rendered PNG. Skips the verification simulation suite (a quick DC sanity check only) and behavioral models."
                : spec.deliverable === "symbol"
                ? "Symbol-only run: just the xschem .sym for this block (pins from the filed library schematic if the cell exists, else from the spec). Cheap and fast - runs directly, no research step, no simulations."
                : "Model-only run: generates just this behavioral model from the spec (no transistor-level design), verified with the local toolchain. Cheap and fast - runs directly, no research step."}
            </p>
            {toolWarning && <p className="field-warning">{toolWarning}</p>}
          </>
        );
      })()}

      {!spec.library && (
        <p className="hint">Choose (or create) a design library above to enable the build.</p>
      )}
      <button type="submit" disabled={submitting || !spec.library}>
        {submitting
          ? "Working..."
          : SUBMIT_LABELS[spec.deliverable] ||
            (isCurrentMirror ? "Build current mirror" : "Research topology options")}
      </button>
      {settings?.derived?.runs && (
        <p className="hint runs-root-hint">
          Run artifacts (schematic, netlist, logs) will be created under{" "}
          <code>{settings.derived.runs}/&lt;run_id&gt;/</code> — configurable in Settings.
        </p>
      )}
        </>
      )}
    </form>
  );
}
