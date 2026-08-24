import { useEffect, useMemo, useState } from "react";
import { listInterfaces, saveInterface } from "./api";
import {
  ASYNC_DOMAIN,
  DIRECTIONS,
  HANDSHAKES,
  RATES,
  SIGNAL_GROUPS,
  defaultInterfaceForPhy,
  loadIfDef,
  saveIfDef,
  resetIfDef,
  validateIfDef,
} from "./interfaceStore";

// AFE<->digital interface table editor (2026-08-23 digital-arch spec,
// section c): the middle workspace panel. Renders the working-copy interface
// definition (localStorage `if-def-<phy>`, seeded from the shipped per-PHY
// default) as grouped signal tables + clock-domain/CDC/sequence tables, all
// editable in place with the enum dropdowns, inline soft warnings (same
// visual language as the spec form's sky130 warnings) and inline hard errors
// (the server re-checks on save - 422s come back formatted).
//
// Chat patches (if_chat) are applied by the chat sidebar through
// interfaceStore.applyIfOps against the SAME working copy; `version` bumps
// when that happens so this editor re-reads. The editor's own edits write
// straight to the working copy (the sidebar reads it fresh at send time), so
// they don't need a version bump.

function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

function freshSignalName(signals) {
  const names = new Set(signals.map((s) => s.name));
  let i = 1;
  while (names.has(`new_signal${i > 1 ? i : ""}`)) i += 1;
  return `new_signal${i > 1 ? i : ""}`;
}

function freshDomainName(domains) {
  const names = new Set(domains.map((d) => d.name));
  let i = 1;
  while (names.has(`clk_new${i > 1 ? i : ""}`)) i += 1;
  return `clk_new${i > 1 ? i : ""}`;
}

function RowIssues({ issues }) {
  if (!issues || (!issues.errors.length && !issues.warnings.length)) return null;
  return (
    <tr className="if-issue-row">
      <td colSpan={99}>
        {issues.errors.map((e, i) => (
          <span key={`e${i}`} className="if-error">✕ {e}</span>
        ))}
        {issues.warnings.map((w, i) => (
          <span key={`w${i}`} className="if-warning">⚠ {w}</span>
        ))}
      </td>
    </tr>
  );
}

export default function InterfaceEditor({ phyType, version }) {
  const [def, setDef] = useState(() => loadIfDef(phyType));
  const [status, setStatus] = useState(null); // {kind: "info"|"error"|"warn", text}
  const [savedList, setSavedList] = useState(null); // null = not fetched, [] = fetched empty
  const [showRaw, setShowRaw] = useState(false);

  // Re-read the working copy when the PHY changes or an external edit (chat
  // patch apply/undo) bumped `version`.
  useEffect(() => {
    setDef(loadIfDef(phyType));
    setStatus(null);
  }, [phyType, version]);

  const validation = useMemo(() => validateIfDef(def), [def]);
  const domainOptions = [
    ...(def.clock_domains || []).map((d) => d.name).filter(Boolean),
    ASYNC_DOMAIN,
  ];

  function update(next) {
    setDef(next);
    saveIfDef(phyType, next);
  }

  // --- signal helpers (signals keep their original array index) ---
  function setSignal(idx, key, value) {
    const signals = def.signals.map((s, i) => (i === idx ? { ...s, [key]: value } : s));
    update({ ...def, signals });
  }

  function removeSignal(idx) {
    const name = def.signals[idx]?.name;
    update({
      ...def,
      signals: def.signals.filter((_, i) => i !== idx),
      cdc_points: (def.cdc_points || []).filter((c) => c.signal !== name),
    });
  }

  function addSignal(group) {
    const sig = {
      name: freshSignalName(def.signals),
      direction: group === "control" || group === "power" ? "d2a" : "a2d",
      width: 1,
      clock_domain: def.clock_domains[0]?.name || ASYNC_DOMAIN,
      rate: group === "control" || group === "power" ? "quasi_static" : "word",
      rate_mhz: group === "control" || group === "power" ? null : 100,
      group,
      handshake: group === "control" || group === "power" ? "level" : "none",
      afe_block: null,
      digital_block: null,
      meaning: "",
    };
    update({ ...def, signals: [...def.signals, sig] });
  }

  function setDomain(idx, key, value) {
    const clock_domains = def.clock_domains.map((d, i) => (i === idx ? { ...d, [key]: value } : d));
    update({ ...def, clock_domains });
  }

  function removeDomain(idx) {
    update({ ...def, clock_domains: def.clock_domains.filter((_, i) => i !== idx) });
  }

  function addDomain() {
    update({
      ...def,
      clock_domains: [
        ...def.clock_domains,
        { name: freshDomainName(def.clock_domains), freq_mhz: 100, owner: "external", source_block: null, description: "" },
      ],
    });
  }

  function setCdc(idx, key, value) {
    const cdc_points = def.cdc_points.map((c, i) => (i === idx ? { ...c, [key]: value } : c));
    update({ ...def, cdc_points });
  }

  function removeCdc(idx) {
    update({ ...def, cdc_points: def.cdc_points.filter((_, i) => i !== idx) });
  }

  function addCdc() {
    update({
      ...def,
      cdc_points: [
        ...def.cdc_points,
        {
          signal: def.signals[0]?.name || "",
          from_domain: ASYNC_DOMAIN,
          to_domain: def.clock_domains[0]?.name || ASYNC_DOMAIN,
          strategy: "2ff",
          notes: "",
        },
      ],
    });
  }

  function setSeqStep(key, idx, field, value) {
    const steps = def[key].map((s, i) => (i === idx ? { ...s, [field]: value } : s));
    update({ ...def, [key]: steps });
  }

  function moveSeqStep(key, idx, delta) {
    const steps = [...def[key]];
    const j = idx + delta;
    if (j < 0 || j >= steps.length) return;
    [steps[idx], steps[j]] = [steps[j], steps[idx]];
    update({ ...def, [key]: steps.map((s, i) => ({ ...s, step: i + 1 })) });
  }

  function removeSeqStep(key, idx) {
    update({
      ...def,
      [key]: def[key].filter((_, i) => i !== idx).map((s, i) => ({ ...s, step: i + 1 })),
    });
  }

  function addSeqStep(key) {
    update({
      ...def,
      [key]: [...def[key], { step: def[key].length + 1, action: "", signal: null, wait_for: "" }],
    });
  }

  // --- toolbar actions ---
  async function handleSave() {
    const name = window.prompt(
      "Save the interface definition as (1-64 chars: letters, digits, - _; an existing name is overwritten):",
      def.name || `${phyType}-if`
    );
    if (name == null) return;
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
      setStatus({ kind: "error", text: "Invalid name: use 1-64 letters, digits, - or _ only." });
      return;
    }
    try {
      const entry = await saveInterface({ name, interfaceDef: def, phyType, label: name });
      const warnCount = (entry.warnings || []).length;
      setStatus({
        kind: warnCount ? "warn" : "info",
        text: `Saved as "${entry.name}"${warnCount ? ` with ${warnCount} soft warning(s) (stored with the entry)` : ""}.`,
      });
    } catch (e) {
      setStatus({ kind: "error", text: errText(e) });
    }
  }

  async function handleLoadList() {
    try {
      const d = await listInterfaces();
      setSavedList(d.interfaces || []);
      if (!(d.interfaces || []).length) {
        setStatus({ kind: "info", text: "No saved interface definitions yet - Save creates the first one." });
      }
    } catch (e) {
      setStatus({ kind: "error", text: errText(e) });
    }
  }

  function handleLoadEntry(entry) {
    update({ ...entry.interface });
    setSavedList(null);
    setStatus({ kind: "info", text: `Loaded "${entry.name}" into the working copy.` });
  }

  function handleReset() {
    resetIfDef(phyType);
    setDef(defaultInterfaceForPhy(phyType));
    setStatus({ kind: "info", text: `Reset to the built-in ${phyType} default.` });
  }

  function handleExport() {
    const blob = new Blob([JSON.stringify(def, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${def.name || `${phyType}-if`}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  const groupedSignals = SIGNAL_GROUPS.map((group) => ({
    group,
    rows: (def.signals || [])
      .map((sig, idx) => ({ sig, idx }))
      .filter(({ sig }) => sig.group === group),
  }));

  return (
    <div className="if-editor">
      <div className="if-toolbar">
        <label className="if-name-label">
          Name
          <input
            type="text"
            value={def.name || ""}
            onChange={(e) => update({ ...def, name: e.target.value })}
          />
        </label>
        <div className="if-toolbar-buttons">
          <button type="button" onClick={handleSave} title="Save server-side (POST /api/interfaces); hard errors block, soft warnings are stored">
            Save
          </button>
          <button type="button" className="toggle-raw" onClick={handleLoadList} title="Load a saved interface definition (GET /api/interfaces)">
            Load
          </button>
          <button type="button" className="toggle-raw" onClick={handleReset} title="Discard the working copy and restore the built-in default for this PHY">
            Reset to default
          </button>
          <button type="button" className="toggle-raw" onClick={handleExport} title="Download the definition verbatim as JSON">
            Export JSON
          </button>
          <button type="button" className="toggle-raw" onClick={() => setShowRaw((r) => !r)}>
            {showRaw ? "Hide JSON" : "View JSON"}
          </button>
        </div>
      </div>

      {status && (
        <p className={status.kind === "error" ? "field-warning" : status.kind === "warn" ? "if-warning" : "hint"}>
          {status.text}
        </p>
      )}

      {savedList && savedList.length > 0 && (
        <div className="if-load-list">
          <strong>Load a saved definition:</strong>
          <ul>
            {savedList.map((entry) => (
              <li key={entry.name}>
                <button type="button" className="toggle-raw" onClick={() => handleLoadEntry(entry)}>
                  {entry.label || entry.name}
                </button>
                <span className="hint"> {entry.phy_type || "?"} · {entry.interface?.signals?.length ?? 0} signals</span>
              </li>
            ))}
          </ul>
          <button type="button" className="toggle-raw" onClick={() => setSavedList(null)}>Close</button>
        </div>
      )}

      {showRaw && (
        <pre className="if-raw-json"><code>{JSON.stringify(def, null, 2)}</code></pre>
      )}

      {validation.allErrors.length > 0 && (
        <p className="field-warning">
          {validation.allErrors.length} hard error(s) below - saving will be rejected until they are fixed.
        </p>
      )}

      {/* --- Signals, one section per group in the canonical order --- */}
      <h4 className="if-section-head">Signals</h4>
      {groupedSignals.map(({ group, rows }) => (
        <div key={group} className="if-group">
          <div className="if-group-head">
            <span className="if-group-name">{group}</span>
            <button type="button" className="toggle-raw" onClick={() => addSignal(group)}>
              + Add {group} signal
            </button>
          </div>
          {rows.length > 0 && (
            <table className="if-table">
              <thead>
                <tr>
                  <th>name</th>
                  <th>dir</th>
                  <th>width</th>
                  <th>clock domain</th>
                  <th>rate</th>
                  <th>MHz</th>
                  <th>handshake</th>
                  <th>afe block</th>
                  <th>digital block</th>
                  <th>meaning</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map(({ sig, idx }) => (
                  [
                    <tr key={idx}>
                      <td>
                        <input
                          type="text"
                          className="if-in-name"
                          value={sig.name || ""}
                          onChange={(e) => setSignal(idx, "name", e.target.value)}
                        />
                        {Number(sig.width) > 1 && (
                          <span className="hint if-width-hint">[{Number(sig.width) - 1}:0]</span>
                        )}
                      </td>
                      <td>
                        <select value={sig.direction} onChange={(e) => setSignal(idx, "direction", e.target.value)}>
                          {DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
                        </select>
                      </td>
                      <td>
                        <input
                          type="number"
                          className="if-in-num"
                          min={1}
                          value={sig.width ?? ""}
                          onChange={(e) => setSignal(idx, "width", e.target.value === "" ? "" : Number(e.target.value))}
                        />
                      </td>
                      <td>
                        <select value={sig.clock_domain || ""} onChange={(e) => setSignal(idx, "clock_domain", e.target.value)}>
                          {!domainOptions.includes(sig.clock_domain) && (
                            <option value={sig.clock_domain || ""}>{sig.clock_domain || "(unset)"}</option>
                          )}
                          {domainOptions.map((d) => <option key={d} value={d}>{d}</option>)}
                        </select>
                      </td>
                      <td>
                        <select value={sig.rate} onChange={(e) => setSignal(idx, "rate", e.target.value)}>
                          {RATES.map((r) => <option key={r} value={r}>{r}</option>)}
                        </select>
                      </td>
                      <td>
                        {sig.rate === "word" ? (
                          <input
                            type="number"
                            className="if-in-num"
                            min={0}
                            value={sig.rate_mhz ?? ""}
                            onChange={(e) => setSignal(idx, "rate_mhz", e.target.value === "" ? null : Number(e.target.value))}
                          />
                        ) : (
                          <span className="hint">—</span>
                        )}
                      </td>
                      <td>
                        <select value={sig.handshake} onChange={(e) => setSignal(idx, "handshake", e.target.value)}>
                          {HANDSHAKES.map((h) => <option key={h} value={h}>{h}</option>)}
                        </select>
                      </td>
                      <td>
                        <input
                          type="text"
                          className="if-in-block"
                          value={sig.afe_block || ""}
                          placeholder="(none)"
                          onChange={(e) => setSignal(idx, "afe_block", e.target.value || null)}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          className="if-in-block"
                          value={sig.digital_block || ""}
                          placeholder="(none)"
                          onChange={(e) => setSignal(idx, "digital_block", e.target.value || null)}
                        />
                      </td>
                      <td>
                        <input
                          type="text"
                          className="if-in-meaning"
                          value={sig.meaning || ""}
                          onChange={(e) => setSignal(idx, "meaning", e.target.value)}
                        />
                      </td>
                      <td>
                        <button type="button" className="toggle-raw if-del" onClick={() => removeSignal(idx)} title="Delete this signal (and its CDC entries)">
                          ✕
                        </button>
                      </td>
                    </tr>,
                    <RowIssues key={`${idx}-issues`} issues={validation.signalIssues[idx]} />,
                  ]
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}

      {/* --- Clock domains --- */}
      <h4 className="if-section-head">Clock domains</h4>
      <table className="if-table">
        <thead>
          <tr>
            <th>name</th><th>freq (MHz)</th><th>owner</th><th>source block</th><th>description</th><th></th>
          </tr>
        </thead>
        <tbody>
          {(def.clock_domains || []).map((dom, idx) => [
            <tr key={idx}>
              <td><input type="text" className="if-in-name" value={dom.name || ""} onChange={(e) => setDomain(idx, "name", e.target.value)} /></td>
              <td><input type="number" className="if-in-num" value={dom.freq_mhz ?? ""} onChange={(e) => setDomain(idx, "freq_mhz", e.target.value === "" ? null : Number(e.target.value))} /></td>
              <td><input type="text" className="if-in-block" value={dom.owner || ""} onChange={(e) => setDomain(idx, "owner", e.target.value)} /></td>
              <td><input type="text" className="if-in-block" value={dom.source_block || ""} placeholder="(none)" onChange={(e) => setDomain(idx, "source_block", e.target.value || null)} /></td>
              <td><input type="text" className="if-in-meaning" value={dom.description || ""} onChange={(e) => setDomain(idx, "description", e.target.value)} /></td>
              <td><button type="button" className="toggle-raw if-del" onClick={() => removeDomain(idx)}>✕</button></td>
            </tr>,
            <RowIssues key={`${idx}-issues`} issues={validation.domainIssues[idx]} />,
          ])}
        </tbody>
      </table>
      <button type="button" className="toggle-raw" onClick={addDomain}>+ Add clock domain</button>

      {/* --- CDC points --- */}
      <h4 className="if-section-head">CDC points</h4>
      <table className="if-table">
        <thead>
          <tr>
            <th>signal</th><th>from domain</th><th>to domain</th><th>strategy</th><th>notes</th><th></th>
          </tr>
        </thead>
        <tbody>
          {(def.cdc_points || []).map((cdc, idx) => [
            <tr key={idx}>
              <td>
                <select value={cdc.signal || ""} onChange={(e) => setCdc(idx, "signal", e.target.value)}>
                  {!def.signals.some((s) => s.name === cdc.signal) && (
                    <option value={cdc.signal || ""}>{cdc.signal || "(unset)"}</option>
                  )}
                  {def.signals.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
                </select>
              </td>
              <td>
                <select value={cdc.from_domain || ""} onChange={(e) => setCdc(idx, "from_domain", e.target.value)}>
                  {domainOptions.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </td>
              <td>
                <select value={cdc.to_domain || ""} onChange={(e) => setCdc(idx, "to_domain", e.target.value)}>
                  {domainOptions.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </td>
              <td><input type="text" className="if-in-block" value={cdc.strategy || ""} onChange={(e) => setCdc(idx, "strategy", e.target.value)} /></td>
              <td><input type="text" className="if-in-meaning" value={cdc.notes || ""} onChange={(e) => setCdc(idx, "notes", e.target.value)} /></td>
              <td><button type="button" className="toggle-raw if-del" onClick={() => removeCdc(idx)}>✕</button></td>
            </tr>,
            <RowIssues key={`${idx}-issues`} issues={validation.cdcIssues[idx]} />,
          ])}
        </tbody>
      </table>
      <button type="button" className="toggle-raw" onClick={addCdc} disabled={!def.signals.length}>+ Add CDC point</button>

      {/* --- Sequences --- */}
      {["reset_sequence", "power_sequence"].map((key) => (
        <div key={key}>
          <h4 className="if-section-head">{key === "reset_sequence" ? "Reset sequence" : "Power sequence"}</h4>
          <table className="if-table">
            <thead>
              <tr><th>step</th><th>action</th><th>signal</th><th>wait for</th><th></th></tr>
            </thead>
            <tbody>
              {(def[key] || []).map((step, idx) => [
                <tr key={idx}>
                  <td className="if-step-num">{step.step ?? idx + 1}</td>
                  <td><input type="text" className="if-in-meaning" value={step.action || ""} onChange={(e) => setSeqStep(key, idx, "action", e.target.value)} /></td>
                  <td><input type="text" className="if-in-block" value={step.signal || ""} placeholder="(none)" onChange={(e) => setSeqStep(key, idx, "signal", e.target.value || null)} /></td>
                  <td><input type="text" className="if-in-block" value={step.wait_for || ""} onChange={(e) => setSeqStep(key, idx, "wait_for", e.target.value)} /></td>
                  <td className="if-seq-actions">
                    <button type="button" className="toggle-raw" onClick={() => moveSeqStep(key, idx, -1)} disabled={idx === 0} title="Move up">↑</button>
                    <button type="button" className="toggle-raw" onClick={() => moveSeqStep(key, idx, 1)} disabled={idx === (def[key]?.length || 0) - 1} title="Move down">↓</button>
                    <button type="button" className="toggle-raw if-del" onClick={() => removeSeqStep(key, idx)}>✕</button>
                  </td>
                </tr>,
                <RowIssues key={`${idx}-issues`} issues={validation.sequenceIssues[key][idx]} />,
              ])}
            </tbody>
          </table>
          <button type="button" className="toggle-raw" onClick={() => addSeqStep(key)}>+ Add step</button>
        </div>
      ))}
    </div>
  );
}
