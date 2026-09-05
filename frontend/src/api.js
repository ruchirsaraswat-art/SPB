// Relative by default (single-origin mode: FastAPI serves this built
// frontend itself, see backend/main.py's StaticFiles mount + SPA fallback,
// mounted AFTER every /api route). VITE_API_BASE overrides this for anyone
// who still wants split dev servers pointed at a different backend host -
// `npm run dev`'s own split-server case is handled instead by the Vite proxy
// in vite.config.js, so relative paths already work there without setting
// this.
const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// ---------------------------------------------------------------------------
// Shared-secret auth (remote-access hardening, see backend/auth.py). The
// token is entered once by the user (TokenGate.jsx, shown on any 401) and
// kept in localStorage; every request below sends it as X-Auth-Token. Never
// logged to the console. A loopback caller (the pre-existing local
// workflow) never needs a token at all - the backend exempts it - so this
// is a no-op there (header sent but ignored/unnecessary).
const TOKEN_STORAGE_KEY = "analog-spec-tool-auth-token";

export function getAuthToken() {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return ""; // localStorage unavailable (private-mode Safari etc.)
  }
}

export function setAuthToken(token) {
  try {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    /* not persisted this session - still used for in-memory requests below */
  }
}

// Fired on ANY 401 from the API, regardless of which panel/action triggered
// the call, so App.jsx can show the token-entry screen from one place
// instead of every call site handling it separately.
const AUTH_REQUIRED_EVENT = "analog-spec-tool:auth-required";

export function onAuthRequired(callback) {
  window.addEventListener(AUTH_REQUIRED_EVENT, callback);
  return () => window.removeEventListener(AUTH_REQUIRED_EVENT, callback);
}

// Drop-in replacement for `fetch` used by every call below: adds the
// X-Auth-Token header when a token is stored, and raises the auth-required
// event on a 401 so the app can prompt for a (new) token - the caller's own
// `!res.ok` handling still runs afterwards unchanged (a 401 still surfaces
// as a normal "action failed" error to whatever triggered it, in addition
// to the app-wide prompt).
async function apiFetch(url, options = {}) {
  const token = getAuthToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers["X-Auth-Token"] = token;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent(AUTH_REQUIRED_EVENT));
  }
  return res;
}

// Turn an error response into a readable message. FastAPI's 422 body is
// {"detail": [{loc, msg, type, ...}, ...]} - rendering that raw JSON in the
// error box is unreadable, so format one clear line per failed field
// instead: "<field path>: <message>". Non-validation errors (string detail,
// non-JSON body) fall back to the raw text.
async function apiErrorMessage(res, action) {
  const text = await res.text().catch(() => "");
  let body = null;
  try {
    body = JSON.parse(text);
  } catch {
    /* not JSON - fall through to the raw-text fallback */
  }
  const detail = body?.detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const lines = detail.map((item) => {
      // Drop the constant "body" prefix from the location path; what's left
      // is the actual field (e.g. "extra_fields" or "temp_c"). Model-level
      // validators report at the bare body, so the path can be empty - the
      // message itself names the field then.
      const loc = (item.loc || []).filter((p) => p !== "body").join(".");
      // Pydantic v2 prefixes custom validator failures with "Value error, ".
      const msg = String(item.msg || "").replace(/^Value error,\s*/, "");
      return `• ${loc ? `${loc}: ` : ""}${msg}`;
    });
    return `${action}: the spec failed validation (HTTP ${res.status}):\n${lines.join("\n")}`;
  }
  if (typeof detail === "string") {
    return `${action}: ${detail} (HTTP ${res.status})`;
  }
  return `${action}: HTTP ${res.status} ${text}`;
}

export async function createRun(spec) {
  const res = await apiFetch(`${API_BASE}/api/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to create run"));
  }
  return res.json();
}

export async function getRun(runId) {
  const res = await apiFetch(`${API_BASE}/api/runs/${runId}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch run ${runId}: ${res.status}`);
  }
  return res.json();
}

export async function listRuns() {
  const res = await apiFetch(`${API_BASE}/api/runs`);
  if (!res.ok) {
    throw new Error(`Failed to list runs: ${res.status}`);
  }
  return res.json();
}

export function fileUrl(runId, name) {
  return `${API_BASE}/api/runs/${runId}/file/${encodeURIComponent(name)}`;
}

// `kind` selects between a build run's file endpoint and a research run's -
// same session.log/claude_stream.jsonl shape, different id namespace.
export async function fetchLog(id, name = "session.log", kind = "run") {
  const url = kind === "research" ? researchFileUrl(id, name) : fileUrl(id, name);
  const res = await apiFetch(url);
  if (!res.ok) {
    if (res.status === 404) return ""; // log not written yet
    throw new Error(`Failed to fetch log ${name} for ${kind} ${id}: ${res.status}`);
  }
  return res.text();
}

export async function cancelRun(runId) {
  const res = await apiFetch(`${API_BASE}/api/runs/${runId}/cancel`, { method: "POST" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to cancel run: ${res.status} ${body}`);
  }
  return res.json();
}

export async function openSchematic(runId) {
  const res = await apiFetch(`${API_BASE}/api/runs/${runId}/open-schematic`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to open schematic: ${res.status}`);
  }
  return body;
}

// Hierarchical design libraries (libraries/<lib>/<cell>/<views> on the
// backend, browsable from xschem via XSCHEM_LIBRARY_PATH).
export async function listLibraries() {
  const res = await apiFetch(`${API_BASE}/api/libraries`);
  if (!res.ok) {
    throw new Error(`Failed to list libraries: ${res.status}`);
  }
  return res.json();
}

export async function createLibrary(name) {
  const res = await apiFetch(`${API_BASE}/api/libraries`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to create library"));
  }
  return res.json();
}

// Which model-verification tools (openvaf, iverilog/vvp) are installed on
// the backend machine - drives the "will be generated but unverified"
// inline warnings next to the behavioral-model checkboxes.
export async function getToolchain() {
  const res = await apiFetch(`${API_BASE}/api/toolchain`);
  if (!res.ok) {
    throw new Error(`Failed to fetch toolchain status: ${res.status}`);
  }
  return res.json();
}

export async function getTopologies() {
  const res = await apiFetch(`${API_BASE}/api/topologies`);
  if (!res.ok) {
    throw new Error(`Failed to fetch topologies: ${res.status}`);
  }
  return res.json();
}

export async function createResearch(spec) {
  const res = await apiFetch(`${API_BASE}/api/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(spec),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to start research"));
  }
  return res.json();
}

export async function getResearch(researchId) {
  const res = await apiFetch(`${API_BASE}/api/research/${researchId}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch research ${researchId}: ${res.status}`);
  }
  return res.json();
}

export async function cancelResearch(researchId) {
  const res = await apiFetch(`${API_BASE}/api/research/${researchId}/cancel`, { method: "POST" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to cancel research: ${res.status} ${body}`);
  }
  return res.json();
}

export function researchFileUrl(researchId, name) {
  return `${API_BASE}/api/research/${researchId}/file/${encodeURIComponent(name)}`;
}

// Schematic sub-window (cycle 4): backend-resolved "best schematic for this
// topology" - filed library cell first, else latest successful run's PNG,
// else {found:false, message} (the defined empty state). The response's
// png_url is an API-relative path; join it with apiUrl() for the <img> src.
export async function resolveSchematic(topology) {
  const res = await apiFetch(`${API_BASE}/api/schematic/resolve/${encodeURIComponent(topology)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to resolve schematic"));
  }
  return res.json();
}

// Results sub-window: backend-resolved "best results for this topology" -
// same filed-cell-beats-latest-run priority as resolveSchematic, just
// carrying measurements/logs/waveform-availability instead of a rendered
// view. Always resolves to {found:false, message} rather than a 404 when
// nothing exists yet - the frontend renders that verbatim.
export async function resolveResults(topology) {
  const res = await apiFetch(`${API_BASE}/api/results/resolve/${encodeURIComponent(topology)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to resolve results"));
  }
  return res.json();
}

// Parsed ngspice ascii .raw waveform data for one artifact, from whichever
// base URL the resolved result carried (a run's or a library cell's -
// resolve_results already picked the right one server-side, this just
// appends the filename). See backend/rawfile.py for supported-format/size
// caveats surfaced as the thrown error message on a non-2xx response.
export async function fetchWaveform(waveformUrlBase, name) {
  const res = await apiFetch(`${API_BASE}${waveformUrlBase}${encodeURIComponent(name)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, `Failed to load waveform ${name}`));
  }
  return res.json();
}

// Spawn interactive xschem on the topology's resolved schematic (backend
// re-resolves; 409 when no X display is reachable at click time).
export async function openTopologySchematic(topology) {
  const res = await apiFetch(`${API_BASE}/api/schematic/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topology }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to open schematic in xschem: ${res.status}`);
  }
  return body;
}

// Draw a new schematic by hand (2026-09): for a block with no resolved
// schematic yet (resolveSchematic() returned found:false). Creates
// libraries/<library>/<cell>/<cell>.sch as a blank-but-valid xschem file -
// same library/cell convention as the spec form's library picker, just
// reached from the schematic panel's empty state instead.

// Thrown specifically on a 409 (cell already has a .sch) so the caller can
// offer "open it" instead of just showing a generic error string.
export class CellExistsError extends Error {
  constructor(message, library, cell) {
    super(message);
    this.name = "CellExistsError";
    this.library = library;
    this.cell = cell;
  }
}

export async function createBlankSchematic(library, cell, topology, label) {
  const res = await apiFetch(`${API_BASE}/api/schematic/new-cell`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ library, cell, topology, label: label || null }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 409 && body.detail?.library) {
      throw new CellExistsError(
        body.detail.message || "That cell already has a schematic",
        body.detail.library,
        body.detail.cell
      );
    }
    throw new Error(body.detail || `Failed to create schematic: ${res.status}`);
  }
  return body;
}

// Live xschem session on a SPECIFIC library cell (not topology-resolved) -
// the counterpart to startXschemSession above for a cell resolveSchematic
// can't find yet (freshly created, or not captured since the last edit).
export async function startXschemCellSession(library, cell) {
  const res = await apiFetch(`${API_BASE}/api/xschem/sessions/cell`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ library, cell }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to start xschem session: ${res.status}`);
  }
  return body;
}

// Headlessly render the cell's current .sch to a PNG, extract its netlist,
// and update its provenance so resolveSchematic finds it from now on. Call
// after saving from a live session; safe to call again after further edits.
export async function captureSchematic(library, cell) {
  const res = await apiFetch(`${API_BASE}/api/schematic/capture`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ library, cell }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to capture schematic: ${res.status}`);
  }
  return body;
}

// Interactive xschem-over-VNC sessions (2026-09 remote-access spec): the
// browser-reachable alternative to openTopologySchematic above, for anyone
// reaching this tool remotely (Tailscale etc) instead of sitting at the
// backend machine's own desktop. See backend/xschem_session.py.
export async function xschemPrereqs() {
  const res = await apiFetch(`${API_BASE}/api/xschem/prereqs`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to check xschem prerequisites"));
  }
  return res.json();
}

// Starts (or reuses) a session for the topology's resolved schematic.
// Returns {session_id, vnc_password, ...} - vnc_password is a ONE-TIME value
// (never returned again by getXschemSession), handed straight to the noVNC
// client so the RFB handshake needs no separate user prompt.
export async function startXschemSession(topology) {
  const res = await apiFetch(`${API_BASE}/api/xschem/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topology }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to start xschem session: ${res.status}`);
  }
  return body;
}

export async function getXschemSession(sessionId) {
  const res = await apiFetch(`${API_BASE}/api/xschem/sessions/${encodeURIComponent(sessionId)}`);
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to check xschem session"));
  }
  return res.json();
}

export async function stopXschemSession(sessionId) {
  const res = await apiFetch(`${API_BASE}/api/xschem/sessions/${encodeURIComponent(sessionId)}/stop`, {
    method: "POST",
  });
  if (!res.ok && res.status !== 404) {
    throw new Error(await apiErrorMessage(res, "Failed to stop xschem session"));
  }
  return true;
}

// WebSocket URL for the noVNC client - carries the auth token as a ?token=
// query param because the browser's native WebSocket API cannot set custom
// headers (X-Auth-Token, what every other call above uses); the backend's
// shared-secret gate already accepts a valid query token on any path (see
// backend/auth.py), the same mechanism the very first page load uses.
export function xschemWsUrl(sessionId) {
  const base = API_BASE || window.location.origin;
  const wsBase = base.replace(/^http/, "ws");
  const token = getAuthToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${wsBase}/api/xschem/sessions/${encodeURIComponent(sessionId)}/ws${q}`;
}

// Tool settings (cycle 5): the working directory the tool creates its data
// under (libraries/, runs/, research_runs/), with derived sub-paths and any
// previous locations that still hold data.
export async function getSettings() {
  const res = await apiFetch(`${API_BASE}/api/settings`);
  if (!res.ok) {
    throw new Error(`Failed to fetch settings: ${res.status}`);
  }
  return res.json();
}

export async function saveSettings(workingDir, librariesDir) {
  const res = await apiFetch(`${API_BASE}/api/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ working_dir: workingDir, libraries_dir: librariesDir || null }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to save settings"));
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Architecture-discussion chat (drawing tab). Full endpoint/patch contract:
// audits/2026-08-21-arch-chat-feature.md.

// One chat turn with the architecture consultant. SYNCHRONOUS on the
// backend: this promise resolves only when the consultant has answered
// (typically 20-90 s) - the sidebar must show a pending state and disable
// re-submit meanwhile. `architecture` is the diagram as currently displayed:
// {blocks: [{id, label, topology, ...}], edges: [{from, to}]}.
// Resolves to {session_id, reply, patch, patch_valid, patch_errors,
// cost_usd, duration_ms}; `patch` is {ops: [...]} (ops may carry a
// per-op `error` field when patch_valid is false) or null.
// `view` (2026-08-23 digital-arch spec): "afe" (default) validates patch
// topologies against the analog catalog; "digital" against the digital block
// catalog and frames the consultant as a digital-microarchitecture chat -
// `afeArchitecture` is then sent as READ-ONLY prompt context.
export async function archChat({ sessionId, message, architecture, phyType, view, afeArchitecture }) {
  const res = await apiFetch(`${API_BASE}/api/arch_chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      architecture,
      phy_type: phyType ?? null,
      view: view ?? "afe",
      afe_architecture: afeArchitecture ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Architecture chat failed"));
  }
  return res.json();
}

// Persisted transcript for a chat session ({session_id, created_at, turns:
// [{role, content, patch?, ts}]}); an unknown id returns an empty
// transcript - used to restore the sidebar history after a reload.
export async function getArchChatTranscript(sessionId) {
  const res = await apiFetch(`${API_BASE}/api/arch_chat/${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to fetch chat transcript"));
  }
  return res.json();
}

// Save (or overwrite - same name = ordinary re-save) a named custom
// architecture. Resolves to the stored entry
// {name, label, phy_type, created_at, updated_at?, architecture}.
export async function saveArchitecture({ name, architecture, phyType, label }) {
  const res = await apiFetch(`${API_BASE}/api/architectures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, architecture, phy_type: phyType ?? null, label: label ?? null }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to save architecture"));
  }
  return res.json();
}

// User-saved custom architectures, newest first:
// {architectures: [{name, label, phy_type, created_at, architecture}, ...]}.
// Built-ins stay frontend-static in phyArchitectures.js - the selector UI
// merges this list alongside PHY_ARCHITECTURES.
export async function listArchitectures() {
  const res = await apiFetch(`${API_BASE}/api/architectures`);
  if (!res.ok) {
    throw new Error(`Failed to list architectures: ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Digital block catalog + AFE<->digital interface workspace (2026-08-23
// digital-arch spec). Contracts: backend/digital_blocks.py, interfaces.py,
// if_chat.py.

// Digital block catalog: {groups: [{group, options: [{value, label, fields,
// models}]}], by_phy: {phyType: [block values]}} - same option shape as
// /api/topologies, separate value namespace.
export async function getDigitalBlocks() {
  const res = await apiFetch(`${API_BASE}/api/digital_blocks`);
  if (!res.ok) {
    throw new Error(`Failed to fetch digital blocks: ${res.status}`);
  }
  return res.json();
}

// User-saved interface definitions, newest first:
// {interfaces: [{name, label, phy_type, created_at, warnings, interface}]}.
export async function listInterfaces() {
  const res = await apiFetch(`${API_BASE}/api/interfaces`);
  if (!res.ok) {
    throw new Error(`Failed to list interfaces: ${res.status}`);
  }
  return res.json();
}

// Save (or overwrite - same name = ordinary re-save) a named interface
// definition. Hard validation errors are a 422 (formatted by
// apiErrorMessage); soft warnings come back in the entry's "warnings".
export async function saveInterface({ name, interfaceDef, phyType, label }) {
  const res = await apiFetch(`${API_BASE}/api/interfaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      interface: interfaceDef,
      phy_type: phyType ?? null,
      label: label ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to save interface"));
  }
  return res.json();
}

// One interface-consultant turn. Same synchronous/slow semantics as
// archChat; both architectures are READ-ONLY context. Resolves to
// {session_id, reply, patch, patch_valid, patch_errors, interface_warnings,
// cost_usd, duration_ms}; patch ops are the interface set (add_signal,
// update_clock_domain, set_power_sequence, ...).
export async function ifChat({ sessionId, message, interfaceDef, afeArchitecture, digitalArchitecture, phyType }) {
  const res = await apiFetch(`${API_BASE}/api/if_chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      interface: interfaceDef,
      afe_architecture: afeArchitecture ?? null,
      digital_architecture: digitalArchitecture ?? null,
      phy_type: phyType ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Interface chat failed"));
  }
  return res.json();
}

// Persisted transcript for one interface-chat session (if_chats/ namespace,
// separate from arch chats); unknown ids return an empty transcript.
export async function getIfChatTranscript(sessionId) {
  const res = await apiFetch(`${API_BASE}/api/if_chat/${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to fetch interface chat transcript"));
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Firmware workspace (2026-08-23 firmware-section spec). Contracts:
// backend/firmware_blocks.py, fw_chat.py, fw_generate.py.

// Firmware block catalog: {groups, by_phy, note} - same option shape as
// /api/digital_blocks but with deliberately NO "models" key on any option
// (firmware is host-compiled C, verified by gcc + unit tests, not
// behavioral-model levels - the "note" says so).
export async function getFirmwareBlocks() {
  const res = await apiFetch(`${API_BASE}/api/firmware_blocks`);
  if (!res.ok) {
    throw new Error(`Failed to fetch firmware blocks: ${res.status}`);
  }
  return res.json();
}

// One firmware-consultant turn (Firmware_Coder persona). Same
// synchronous/slow semantics as archChat; the interface definition goes
// along as READ-ONLY context (the patch can only touch the firmware
// diagram). Resolves to {session_id, reply, patch, patch_valid,
// patch_errors, patch_warnings, cost_usd, duration_ms}.
export async function fwChat({ sessionId, message, firmwareArchitecture, interfaceDef, phyType }) {
  const res = await apiFetch(`${API_BASE}/api/fw_chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      firmware_architecture: firmwareArchitecture,
      interface: interfaceDef ?? null,
      phy_type: phyType ?? null,
    }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Firmware chat failed"));
  }
  return res.json();
}

// Persisted transcript for one firmware-chat session (fw_chats/ namespace);
// unknown ids return an empty transcript.
export async function getFwChatTranscript(sessionId) {
  const res = await apiFetch(`${API_BASE}/api/fw_chat/${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to fetch firmware chat transcript"));
  }
  return res.json();
}

// The one v1 firmware codegen action: SYNCHRONOUS on the backend (a paid
// Firmware_Coder run, typically minutes) - the caller must show a pending
// state and disable re-submit meanwhile. Resolves to {status: "pass"|"fail",
// run_id, reason, files, missing_files, compile_ok, tests_ok, test_output,
// cost_usd, duration_s}; pass/fail is server-enforced (the backend re-runs
// gcc -std=c99 -Wall -Werror + make test itself).
export async function fwGenerate({ phyType, interfaceDef }) {
  const res = await apiFetch(`${API_BASE}/api/fw_generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phy_type: phyType, interface: interfaceDef }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Firmware generation failed"));
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Spec-document intake + RTL generation (2026-08-23 rtl-gen spec). Contracts:
// backend/spec_docs.py, backend/rtl_generate.py.

// All spec-doc metadata + per-PHY answer status:
// {docs: [{id, title, standard_family, version, source, user_notes,
// relevant_sections, applies_to_workspaces, phy_type, added_at}],
// status: {phyType: {answer: "attached"|"no_spec"|null, answered_at}}}.
export async function listSpecDocs(phyType) {
  const q = phyType ? `?phy_type=${encodeURIComponent(phyType)}` : "";
  const res = await apiFetch(`${API_BASE}/api/spec_docs${q}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to list spec docs"));
  }
  return res.json();
}

// Attach one spec doc. `metadata.source` is {type: "file", path: "<absolute
// local path the backend copies in>"} | {type: "url", url} |
// {type: "reference", citation}. Also records the per-PHY answer "attached"
// (suppresses the intake banner).
export async function createSpecDoc({ phyType, metadata }) {
  const res = await apiFetch(`${API_BASE}/api/spec_docs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phy_type: phyType, metadata }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to attach spec doc"));
  }
  return res.json();
}

export async function updateSpecDoc({ phyType, docId, metadata }) {
  const res = await apiFetch(
    `${API_BASE}/api/spec_docs/${encodeURIComponent(phyType)}/${encodeURIComponent(docId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metadata }),
    }
  );
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to update spec doc"));
  }
  return res.json();
}

export async function deleteSpecDoc({ phyType, docId }) {
  const res = await apiFetch(
    `${API_BASE}/api/spec_docs/${encodeURIComponent(phyType)}/${encodeURIComponent(docId)}`,
    { method: "DELETE" }
  );
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to remove spec doc"));
  }
  return res.json();
}

// Set the per-PHY spec-doc answer ("no_spec" or "attached") - an explicit
// choice means the intake banner never auto-asks again for this PHY.
export async function setSpecDocAnswer({ phyType, answer }) {
  const res = await apiFetch(
    `${API_BASE}/api/spec_docs/${encodeURIComponent(phyType)}/answer`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    }
  );
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to record spec-doc answer"));
  }
  return res.json();
}

// Kick off an RTL generation run (deliverable "rtl": scope "block" for one
// designable diagram block, "controller" for every block + the stitched
// top). Returns {run_id} immediately - poll GET /api/runs/{id} like every
// other run; the verdict is server-enforced (backend re-runs iverilog+vvp
// itself; controller runs may come back "partial").
export async function rtlGenerate(request) {
  const res = await apiFetch(`${API_BASE}/api/rtl_generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to start RTL generation"));
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// DDR channel models (backend/channels.py). Importing a Touchstone file
// vector-fits it, which takes tens of seconds to minutes, so importChannel
// returns as soon as the job is queued and the panel polls getChannel until
// state === "done". `cached: true` in the response means an identical file
// with identical fit parameters was already fitted - no work was done.

export async function importChannel({ content, filename, path, name, fitParams }) {
  const res = await apiFetch(`${API_BASE}/api/channels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, filename, path, name, fit_params: fitParams }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to import channel"));
  }
  return res.json();
}

export async function listChannels() {
  const res = await apiFetch(`${API_BASE}/api/channels`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to list channels"));
  }
  return res.json();
}

export async function getChannel(channelId) {
  const res = await apiFetch(`${API_BASE}/api/channels/${encodeURIComponent(channelId)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to load channel"));
  }
  return res.json();
}

// Raw artifact text (channel.sp / cursors.txt / taps.hex / fit.log) - shown in
// the panel's raw view so a suspicious fit can actually be inspected.
export async function fetchChannelFile(channelId, name) {
  const res = await apiFetch(
    `${API_BASE}/api/channels/${encodeURIComponent(channelId)}/file/${encodeURIComponent(name)}`
  );
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, `Failed to load ${name}`));
  }
  return res.text();
}

export function apiUrl(path) {
  return `${API_BASE}${path}`;
}
