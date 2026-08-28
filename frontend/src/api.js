const API_BASE = "http://127.0.0.1:8000";

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
  const res = await fetch(`${API_BASE}/api/runs`, {
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
  const res = await fetch(`${API_BASE}/api/runs/${runId}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch run ${runId}: ${res.status}`);
  }
  return res.json();
}

export async function listRuns() {
  const res = await fetch(`${API_BASE}/api/runs`);
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
  const res = await fetch(url);
  if (!res.ok) {
    if (res.status === 404) return ""; // log not written yet
    throw new Error(`Failed to fetch log ${name} for ${kind} ${id}: ${res.status}`);
  }
  return res.text();
}

export async function cancelRun(runId) {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/cancel`, { method: "POST" });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Failed to cancel run: ${res.status} ${body}`);
  }
  return res.json();
}

export async function openSchematic(runId) {
  const res = await fetch(`${API_BASE}/api/runs/${runId}/open-schematic`, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `Failed to open schematic: ${res.status}`);
  }
  return body;
}

// Hierarchical design libraries (libraries/<lib>/<cell>/<views> on the
// backend, browsable from xschem via XSCHEM_LIBRARY_PATH).
export async function listLibraries() {
  const res = await fetch(`${API_BASE}/api/libraries`);
  if (!res.ok) {
    throw new Error(`Failed to list libraries: ${res.status}`);
  }
  return res.json();
}

export async function createLibrary(name) {
  const res = await fetch(`${API_BASE}/api/libraries`, {
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
  const res = await fetch(`${API_BASE}/api/toolchain`);
  if (!res.ok) {
    throw new Error(`Failed to fetch toolchain status: ${res.status}`);
  }
  return res.json();
}

export async function getTopologies() {
  const res = await fetch(`${API_BASE}/api/topologies`);
  if (!res.ok) {
    throw new Error(`Failed to fetch topologies: ${res.status}`);
  }
  return res.json();
}

export async function createResearch(spec) {
  const res = await fetch(`${API_BASE}/api/research`, {
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
  const res = await fetch(`${API_BASE}/api/research/${researchId}`);
  if (!res.ok) {
    throw new Error(`Failed to fetch research ${researchId}: ${res.status}`);
  }
  return res.json();
}

export async function cancelResearch(researchId) {
  const res = await fetch(`${API_BASE}/api/research/${researchId}/cancel`, { method: "POST" });
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
  const res = await fetch(`${API_BASE}/api/schematic/resolve/${encodeURIComponent(topology)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to resolve schematic"));
  }
  return res.json();
}

// Spawn interactive xschem on the topology's resolved schematic (backend
// re-resolves; 409 when no X display is reachable at click time).
export async function openTopologySchematic(topology) {
  const res = await fetch(`${API_BASE}/api/schematic/open`, {
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

// Tool settings (cycle 5): the working directory the tool creates its data
// under (libraries/, runs/, research_runs/), with derived sub-paths and any
// previous locations that still hold data.
export async function getSettings() {
  const res = await fetch(`${API_BASE}/api/settings`);
  if (!res.ok) {
    throw new Error(`Failed to fetch settings: ${res.status}`);
  }
  return res.json();
}

export async function saveSettings(workingDir, librariesDir) {
  const res = await fetch(`${API_BASE}/api/settings`, {
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
  const res = await fetch(`${API_BASE}/api/arch_chat`, {
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
  const res = await fetch(`${API_BASE}/api/arch_chat/${encodeURIComponent(sessionId)}`);
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to fetch chat transcript"));
  }
  return res.json();
}

// Save (or overwrite - same name = ordinary re-save) a named custom
// architecture. Resolves to the stored entry
// {name, label, phy_type, created_at, updated_at?, architecture}.
export async function saveArchitecture({ name, architecture, phyType, label }) {
  const res = await fetch(`${API_BASE}/api/architectures`, {
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
  const res = await fetch(`${API_BASE}/api/architectures`);
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
  const res = await fetch(`${API_BASE}/api/digital_blocks`);
  if (!res.ok) {
    throw new Error(`Failed to fetch digital blocks: ${res.status}`);
  }
  return res.json();
}

// User-saved interface definitions, newest first:
// {interfaces: [{name, label, phy_type, created_at, warnings, interface}]}.
export async function listInterfaces() {
  const res = await fetch(`${API_BASE}/api/interfaces`);
  if (!res.ok) {
    throw new Error(`Failed to list interfaces: ${res.status}`);
  }
  return res.json();
}

// Save (or overwrite - same name = ordinary re-save) a named interface
// definition. Hard validation errors are a 422 (formatted by
// apiErrorMessage); soft warnings come back in the entry's "warnings".
export async function saveInterface({ name, interfaceDef, phyType, label }) {
  const res = await fetch(`${API_BASE}/api/interfaces`, {
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
  const res = await fetch(`${API_BASE}/api/if_chat`, {
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
  const res = await fetch(`${API_BASE}/api/if_chat/${encodeURIComponent(sessionId)}`);
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
  const res = await fetch(`${API_BASE}/api/firmware_blocks`);
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
  const res = await fetch(`${API_BASE}/api/fw_chat`, {
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
  const res = await fetch(`${API_BASE}/api/fw_chat/${encodeURIComponent(sessionId)}`);
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
  const res = await fetch(`${API_BASE}/api/fw_generate`, {
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
  const res = await fetch(`${API_BASE}/api/spec_docs${q}`);
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
  const res = await fetch(`${API_BASE}/api/spec_docs`, {
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(
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
  const res = await fetch(`${API_BASE}/api/rtl_generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to start RTL generation"));
  }
  return res.json();
}

export function apiUrl(path) {
  return `${API_BASE}${path}`;
}
