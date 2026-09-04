import { useEffect, useState } from "react";
import { saveSettings } from "./api";

// Tool settings panel (cycle 5): one working directory the tool creates its
// data under (libraries/, runs/, research_runs/ - shown read-only as derived
// paths). Saving validates on the backend (absolute after ~-expansion,
// created if missing, writable, not inside the tool's source trees) and
// takes effect on the next request - no restart. Existing data is never
// moved: previous locations are listed and stay readable.
//
// Focus mode (2026-08-27, user scope decision): the "Focus: AFE only"
// checkbox. State + persistence (localStorage) live in App.jsx; this panel
// just renders the control. Applies immediately, no save button needed.
// It hides the non-AFE workspaces only - it no longer restricts the PHY
// type (2026-08-31: pinning the selector to DDR just read as broken).
export default function SettingsPanel({ settings, onSaved, onClose, error: loadError, focusDdrAfe, onFocusChange }) {
  const [draft, setDraft] = useState(settings?.working_dir || "");
  const [libDraft, setLibDraft] = useState(settings?.libraries_dir || "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [savedTick, setSavedTick] = useState(false);

  useEffect(() => {
    setDraft(settings?.working_dir || "");
  }, [settings?.working_dir]);

  useEffect(() => {
    setLibDraft(settings?.libraries_dir || "");
  }, [settings?.libraries_dir]);

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSavedTick(false);
    try {
      const updated = await saveSettings(draft, libDraft);
      onSaved?.(updated);
      setSavedTick(true);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  const dirty =
    settings &&
    (draft.trim() !== settings.working_dir || libDraft.trim() !== (settings.libraries_dir || ""));

  return (
    <div className="settings-panel">
      <div className="settings-head">
        <h2>Tool settings</h2>
        <button type="button" className="settings-close" onClick={onClose} aria-label="Close settings">
          ✕
        </button>
      </div>

      {loadError && <p className="field-warning">Could not load settings from the backend: {loadError}</p>}

      <label className="focus-mode-toggle">
        <input
          type="checkbox"
          checked={!!focusDdrAfe}
          onChange={(e) => onFocusChange?.(e.target.checked)}
        />
        <span>Focus: AFE only</span>
      </label>
      <p className="hint">
        Narrows the workbench to the analog front end: the interface /
        controller / firmware workspaces are hidden. Every PHY type stays
        selectable. Nothing is deleted — untick to bring the hidden
        workspaces back. Applies immediately and persists across reloads.
      </p>

      {settings && (
        <>
          <label>
            Work area (runs and research runs are created under it; design libraries too, unless
            a libraries location is set below)
            <input
              type="text"
              data-testid="work-area-input"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setSaveError(null);
                setSavedTick(false);
              }}
              spellCheck={false}
            />
          </label>
          {settings.is_default && draft.trim() === settings.working_dir && (
            <p className="hint">Using the default location ({settings.default_working_dir}).</p>
          )}
          {!settings.is_default && (
            <p className="hint">
              Default is <code>{settings.default_working_dir}</code>.
            </p>
          )}

          <label>
            Libraries location (optional - overrides where design libraries are filed)
            <input
              type="text"
              data-testid="libraries-location-input"
              value={libDraft}
              onChange={(e) => {
                setLibDraft(e.target.value);
                setSaveError(null);
                setSavedTick(false);
              }}
              placeholder={settings.default_libraries_dir}
              spellCheck={false}
            />
          </label>
          {settings.libraries_dir_is_default && !libDraft.trim() && (
            <p className="hint">
              Unset - libraries are filed under the work area (<code>{settings.default_libraries_dir}</code>).
            </p>
          )}
          {!settings.libraries_dir_is_default && (
            <p className="hint">
              Overriding the work area's default of <code>{settings.default_libraries_dir}</code>.
            </p>
          )}

          <div className="settings-actions">
            <button type="button" onClick={handleSave} disabled={saving || !dirty}>
              {saving ? "Saving..." : "Save"}
            </button>
            {dirty && (
              <button
                type="button"
                onClick={() => {
                  setDraft(settings.working_dir);
                  setLibDraft(settings.libraries_dir || "");
                }}
                disabled={saving}
              >
                Revert
              </button>
            )}
            {savedTick && !dirty && <span className="settings-saved">Saved - applies to the next run, no restart needed.</span>}
          </div>
          {saveError && <p className="field-warning settings-error">{saveError}</p>}

          <h3>Where things are created</h3>
          <ul className="settings-derived">
            <li>
              Design libraries: <code>{settings.libraries_root}</code>
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
              <h3>Existing data at previous work areas</h3>
              <ul className="settings-derived">
                {settings.previous_working_dirs.map((p) => (
                  <li key={p.working_dir}>
                    <code>{p.working_dir}</code>
                    {p.has_data?.length > 0 ? ` — holds ${p.has_data.join(", ")}` : " — no data found"}
                  </li>
                ))}
              </ul>
              <p className="hint">
                Changing the work area never moves files. Runs and libraries at previous
                locations remain readable (listing, results and schematic resolution search current
                then previous locations); move files by hand if you want them consolidated.
              </p>
            </>
          )}

          {settings.previous_libraries_dirs?.length > 0 && (
            <>
              <h3>Existing libraries at previous libraries locations</h3>
              <ul className="settings-derived">
                {settings.previous_libraries_dirs.map((p) => (
                  <li key={p.libraries_dir}>
                    <code>{p.libraries_dir}</code>
                    {p.has_data ? " — holds filed libraries" : " — no data found"}
                  </li>
                ))}
              </ul>
              <p className="hint">
                Changing the libraries location never moves files either - filed cells at previous
                libraries locations stay readable the same way.
              </p>
            </>
          )}
        </>
      )}
    </div>
  );
}
