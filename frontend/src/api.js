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

// Virtuoso-style design libraries (libraries/<lib>/<cell>/<views> on the
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

export async function saveSettings(workingDir) {
  const res = await fetch(`${API_BASE}/api/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ working_dir: workingDir }),
  });
  if (!res.ok) {
    throw new Error(await apiErrorMessage(res, "Failed to save settings"));
  }
  return res.json();
}

export function apiUrl(path) {
  return `${API_BASE}${path}`;
}
