import { useState } from "react";

// Shown instead of the whole app on a 401 from the API (see api.js's
// onAuthRequired / App.jsx). The tool prints the token (and a
// ?token=<token> URL that skips this screen entirely) to the backend's
// stdout on startup - see backend/main.py's startup event and run.sh.
export default function TokenGate({ onSubmit }) {
  const [value, setValue] = useState("");

  function handleSubmit(e) {
    e.preventDefault();
    const trimmed = value.trim();
    if (trimmed) onSubmit(trimmed);
  }

  return (
    <div className="app-shell" style={{ maxWidth: 480, margin: "10vh auto" }}>
      <h1>SPB-Saraswat PHY Builder</h1>
      <p>
        This tool requires an access token. Paste the token printed on the
        backend's console at startup (or use the paste-and-go URL it printed,
        which includes <code>?token=...</code> and skips this screen).
      </p>
      <form onSubmit={handleSubmit} style={{ display: "flex", gap: 8 }}>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Access token"
          style={{ flex: 1 }}
        />
        <button type="submit" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </div>
  );
}
