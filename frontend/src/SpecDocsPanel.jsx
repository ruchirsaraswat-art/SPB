import { useState } from "react";
import { createSpecDoc, deleteSpecDoc, setSpecDocAnswer, updateSpecDoc } from "./api";

// Specification-document intake for the Controller workspace (2026-08-23
// rtl-gen spec, section a): a compact banner (never a modal - the workspace
// stays usable) asking whether a spec document (JEDEC/PCIe/UCIe/CXL/...)
// exists for this PHY's controller, plus the "Spec docs (N)" manager that
// lists/adds/removes docs and edits their relevant sections. The per-PHY
// answer lives server-side in spec_docs/<phy>/_status.json: an explicit
// choice (attach or "no spec") suppresses the banner forever; the dismiss
// "x" only suppresses it for this browser session (sessionStorage).
//
// File attach is a SERVER-SIDE PATH: the backend and browser share this
// machine, so the user pastes an absolute path and the backend copies the
// file into spec_docs/<phy>/ (the simpler mechanism that works locally).

const FAMILY_SUGGESTIONS = ["JEDEC", "PCIe", "UCIe", "CXL", "Ethernet", "USB", "MIPI", "proprietary", "other"];
const WORKSPACES = ["controller", "interface", "firmware"];

function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

export function bannerDismissKey(phyType) {
  return `spec-docs-dismissed-${phyType}`;
}

function emptySection() {
  return { label: "", locator: "", applies_to: ["controller"] };
}

function SectionsEditor({ sections, onChange }) {
  return (
    <div className="spec-doc-sections">
      <p className="hint">
        Relevant sections - give page ranges: the generator reads ONLY what you list,
        plus a small budget.
      </p>
      {sections.map((sec, i) => (
        <div key={i} className="spec-doc-section-row">
          <input
            type="text"
            placeholder="Label (e.g. Mode registers)"
            value={sec.label}
            onChange={(e) =>
              onChange(sections.map((s, j) => (j === i ? { ...s, label: e.target.value } : s)))
            }
          />
          <input
            type="text"
            placeholder="Locator (e.g. section 7.2, pages 45-58)"
            value={sec.locator}
            onChange={(e) =>
              onChange(sections.map((s, j) => (j === i ? { ...s, locator: e.target.value } : s)))
            }
          />
          {WORKSPACES.map((w) => (
            <label key={w} className="spec-doc-ws-check">
              <input
                type="checkbox"
                checked={(sec.applies_to || []).includes(w)}
                onChange={() =>
                  onChange(
                    sections.map((s, j) =>
                      j === i
                        ? {
                            ...s,
                            applies_to: (s.applies_to || []).includes(w)
                              ? (s.applies_to || []).filter((x) => x !== w)
                              : [...(s.applies_to || []), w],
                          }
                        : s
                    )
                  )
                }
              />
              {w}
            </label>
          ))}
          <button type="button" className="toggle-raw" onClick={() => onChange(sections.filter((_, j) => j !== i))}>
            remove
          </button>
        </div>
      ))}
      <button type="button" className="toggle-raw" onClick={() => onChange([...sections, emptySection()])}>
        + add section
      </button>
    </div>
  );
}

function AddDocForm({ phyType, initialSourceType, onSaved, onCancel }) {
  const [title, setTitle] = useState("");
  const [family, setFamily] = useState("");
  const [version, setVersion] = useState("");
  const [sourceType, setSourceType] = useState(initialSourceType || "file");
  const [sourceValue, setSourceValue] = useState("");
  const [notes, setNotes] = useState("");
  const [sections, setSections] = useState([emptySection()]);
  const [workspaces, setWorkspaces] = useState([...WORKSPACES]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    const source =
      sourceType === "file"
        ? { type: "file", path: sourceValue }
        : sourceType === "url"
        ? { type: "url", url: sourceValue }
        : { type: "reference", citation: sourceValue };
    try {
      const stored = await createSpecDoc({
        phyType,
        metadata: {
          title,
          standard_family: family,
          version,
          source,
          user_notes: notes,
          relevant_sections: sections.filter((s) => s.label.trim() || s.locator.trim()),
          applies_to_workspaces: workspaces,
        },
      });
      onSaved(stored);
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="spec-doc-add-form">
      <div className="field-row">
        <label>
          Title
          <input type="text" value={title} placeholder="e.g. JESD209-4B LPDDR4" onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          Standard family
          <input type="text" list="spec-doc-families" value={family} onChange={(e) => setFamily(e.target.value)} />
          <datalist id="spec-doc-families">
            {FAMILY_SUGGESTIONS.map((f) => (
              <option key={f} value={f} />
            ))}
          </datalist>
        </label>
        <label>
          Version
          <input type="text" value={version} placeholder="e.g. B, Feb 2017" onChange={(e) => setVersion(e.target.value)} />
        </label>
      </div>
      <div className="field-row">
        <label>
          Source
          <select value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
            <option value="file">Local file (path on this machine - copied in)</option>
            <option value="url">URL</option>
            <option value="reference">Reference only (no file on hand)</option>
          </select>
        </label>
        <label className="spec-doc-source-value">
          {sourceType === "file" ? "Absolute file path" : sourceType === "url" ? "URL" : "Citation"}
          <input
            type="text"
            value={sourceValue}
            placeholder={
              sourceType === "file"
                ? "/home/rsarasw1/specs/jesd209-4b.pdf"
                : sourceType === "url"
                ? "https://..."
                : "JESD209-4B section 7 (no file on hand)"
            }
            onChange={(e) => setSourceValue(e.target.value)}
          />
        </label>
      </div>
      <label>
        Notes (what subset applies)
        <input type="text" value={notes} placeholder="e.g. We only implement the x16 single-channel subset." onChange={(e) => setNotes(e.target.value)} />
      </label>
      <div className="spec-doc-ws-row">
        Applies to:
        {WORKSPACES.map((w) => (
          <label key={w} className="spec-doc-ws-check">
            <input
              type="checkbox"
              checked={workspaces.includes(w)}
              onChange={() =>
                setWorkspaces((cur) => (cur.includes(w) ? cur.filter((x) => x !== w) : [...cur, w]))
              }
            />
            {w}
          </label>
        ))}
      </div>
      <SectionsEditor sections={sections} onChange={setSections} />
      {error && <div className="error-box">{error}</div>}
      <div className="spec-doc-form-actions">
        <button type="button" onClick={handleSave} disabled={saving || !title.trim() || !sourceValue.trim()}>
          {saving ? "Saving..." : "Attach document"}
        </button>
        <button type="button" className="toggle-raw" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function DocRow({ phyType, doc, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [sections, setSections] = useState(doc.relevant_sections || []);
  const [notes, setNotes] = useState(doc.user_notes || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await updateSpecDoc({
        phyType,
        docId: doc.id,
        metadata: { relevant_sections: sections, user_notes: notes },
      });
      setEditing(false);
      onChanged();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove() {
    setBusy(true);
    setError(null);
    try {
      await deleteSpecDoc({ phyType, docId: doc.id });
      onChanged();
    } catch (e) {
      setError(errText(e));
      setBusy(false);
    }
  }

  const source = doc.source || {};
  return (
    <div className="spec-doc-row">
      <div className="spec-doc-row-head">
        <strong>{doc.title}</strong>
        <span className="spec-doc-meta">
          {doc.standard_family || "unspecified family"}
          {doc.version ? ` · ${doc.version}` : ""}
          {" · "}
          {source.type === "file" ? source.path : source.type === "url" ? source.url : `reference: ${source.citation}`}
          {doc.unreadable_by_agent ? " · format not readable by the generator" : ""}
        </span>
        <button type="button" className="toggle-raw" onClick={() => setEditing((v) => !v)}>
          {editing ? "Close" : "Edit sections"}
        </button>
        <button type="button" className="toggle-raw" onClick={handleRemove} disabled={busy}>
          Remove
        </button>
      </div>
      {!editing && (doc.relevant_sections || []).length > 0 && (
        <p className="hint">
          Sections: {(doc.relevant_sections || []).map((s) => `${s.label} (${s.locator})`).join("; ")}
        </p>
      )}
      {editing && (
        <div className="spec-doc-edit">
          <label>
            Notes
            <input type="text" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
          <SectionsEditor sections={sections} onChange={setSections} />
          <button type="button" onClick={handleSave} disabled={busy}>
            {busy ? "Saving..." : "Save"}
          </button>
        </div>
      )}
      {error && <div className="error-box">{error}</div>}
    </div>
  );
}

// The whole intake section: banner (when unanswered + not dismissed this
// session + manager closed) and the manager (opened via the "Spec docs (N)"
// toolbar button owned by the parent panel).
export default function SpecDocsPanel({ phyType, docs, status, open, onClose, onChanged }) {
  const [dismissTick, setDismissTick] = useState(0); // re-render after sessionStorage write
  const [adding, setAdding] = useState(null); // initial source type of the add form, or null
  const [answerError, setAnswerError] = useState(null);

  const answered = status?.answer === "attached" || status?.answer === "no_spec";
  let dismissed = false;
  try {
    dismissed = !!sessionStorage.getItem(bannerDismissKey(phyType));
  } catch {
    /* sessionStorage unavailable */
  }
  const showBanner = !answered && !dismissed && !open;

  async function handleNoSpec() {
    setAnswerError(null);
    try {
      await setSpecDocAnswer({ phyType, answer: "no_spec" });
      onChanged();
    } catch (e) {
      setAnswerError(errText(e));
    }
  }

  function handleDismiss() {
    try {
      sessionStorage.setItem(bannerDismissKey(phyType), "1");
    } catch {
      /* ignore */
    }
    setDismissTick(dismissTick + 1);
  }

  return (
    <div className="spec-docs-section">
      {showBanner && (
        <div className="spec-docs-banner">
          <span className="spec-docs-banner-text">
            Is there a specification document for this controller (JEDEC, PCIe, UCIe, CXL,
            Ethernet, proprietary)? RTL generation and the controller chat will use it as the
            requirements source.
          </span>
          <span className="spec-docs-banner-actions">
            <button type="button" onClick={() => setAdding("file")}>
              Attach file
            </button>
            <button type="button" onClick={() => setAdding("reference")}>
              Paste reference
            </button>
            <button type="button" onClick={handleNoSpec}>
              No spec - first principles
            </button>
            <button type="button" className="toggle-raw spec-docs-dismiss" title="Ask again next session" onClick={handleDismiss}>
              ×
            </button>
          </span>
          {answerError && <div className="error-box">{answerError}</div>}
        </div>
      )}

      {adding && (
        <div className="spec-docs-manager">
          <AddDocForm
            phyType={phyType}
            initialSourceType={adding === "reference" ? "reference" : adding}
            onSaved={() => {
              setAdding(null);
              onChanged();
            }}
            onCancel={() => setAdding(null)}
          />
        </div>
      )}

      {open && (
        <div className="spec-docs-manager">
          <div className="spec-docs-manager-head">
            <strong>Specification documents - {phyType}</strong>
            <span className="hint">
              {status?.answer === "no_spec"
                ? "Declared: no spec, first principles."
                : status?.answer === "attached"
                ? "Requirements source for RTL generation and the controller chat."
                : "No answer recorded yet."}
            </span>
            <button type="button" className="toggle-raw" onClick={() => onClose()}>
              Close
            </button>
          </div>
          {(docs || []).length === 0 && <p className="hint">No documents attached.</p>}
          {(docs || []).map((d) => (
            <DocRow key={d.id} phyType={phyType} doc={d} onChanged={onChanged} />
          ))}
          <div className="spec-docs-manager-actions">
            <button type="button" onClick={() => setAdding("file")}>
              Attach file
            </button>
            <button type="button" onClick={() => setAdding("url")}>
              Add URL
            </button>
            <button type="button" onClick={() => setAdding("reference")}>
              Paste reference
            </button>
            {status?.answer !== "no_spec" && (
              <button type="button" className="toggle-raw" onClick={handleNoSpec}>
                Declare "no spec - first principles"
              </button>
            )}
          </div>
          {answerError && <div className="error-box">{answerError}</div>}
        </div>
      )}
    </div>
  );
}
