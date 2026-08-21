import { useEffect, useState } from "react";
import { saveSettings } from "./api";

// Tool settings panel (cycle 5): one working directory the tool creates its
// data under (libraries/, runs/, research_runs/ - shown read-only as derived
// paths). Saving validates on the backend (absolute after ~-expansion,
// created if missing, writable, not inside the tool's source trees) and
// takes effect on the next request - no restart. Existing data is never
// moved: previous locations are listed and stay readable.
export default function SettingsPanel({ settings, onSaved, onClose, error: loadError }) {
  const [draft, setDraft] = useState(settings?.working_dir || "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [savedTick, setSavedTick] = useState(false);

  useEffect(() => {
    setDraft(settings?.working_dir || "");
  }, [settings?.working_dir]);

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSavedTick(false);
    try {
      const updated = await saveSettings(draft);
      onSaved?.(updated);
      setSavedTick(true);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const dirty = settings && draft.trim() !== settings.working_dir;

  return (
    <div className="settings-panel">
      <div className="settings-head">
        <h2>Tool settings</h2>
        <button type="button" className="settings-close" onClick={onClose} aria-label="Close settings">
          ✕
        </button>
      </div>

      {loadError && <p className="field-warning">Could not load settings from the backend: {loadError}</p>}

      {settings && (
        <>
          <label>
            Working directory (runs, research runs and design libraries are created under it)
            <input
              type="text"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setSaveError(null);
                setSavedTick(false);
              }}
              spellCheck={false}
            />
          </label>
          {settings.is_default && !dirty && (
            <p className="hint">Using the default location ({settings.default_working_dir}).</p>
          )}
          {!settings.is_default && (
            <p className="hint">
              Default is <code>{settings.default_working_dir}</code>.
            </p>
          )}
          <div className="settings-actions">
            <button type="button" onClick={handleSave} disabled={saving || !dirty}>
              {saving ? "Saving..." : "Save"}
            </button>
            {dirty && (
              <button type="button" onClick={() => setDraft(settings.working_dir)} disabled={saving}>
                Revert
              </button>
            )}
            {savedTick && !dirty && <span className="settings-saved">Saved - applies to the next run, no restart needed.</span>}
          </div>
          {saveError && <p className="field-warning settings-error">{saveError}</p>}

          <h3>Where things are created</h3>
          <ul className="settings-derived">
            <li>
              Design libraries: <code>{settings.derived.libraries}</code>
            </li>
            <li>
              Simulation runs: <code>{settings.derived.runs}</code>
            </li>
            <li>
              Research runs: <code>{settings.derived.research_runs}</code>
            </li>
          </ul>

          {settings.previous_working_dirs?.length > 0 && (
            <>
              <h3>Existing data at previous locations</h3>
              <ul className="settings-derived">
                {settings.previous_working_dirs.map((p) => (
                  <li key={p.working_dir}>
                    <code>{p.working_dir}</code>
                    {p.has_data?.length > 0 ? ` — holds ${p.has_data.join(", ")}` : " — no data found"}
                  </li>
                ))}
              </ul>
              <p className="hint">
                Changing the working directory never moves files. Runs and libraries at previous
                locations remain readable (listing, results and schematic resolution search current
                then previous locations); move files by hand if you want them consolidated.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}
