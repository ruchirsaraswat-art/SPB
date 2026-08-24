# 2026-08-21 - Architecture-discussion chat (drawing tab): backend + API contract

Feature: a chat sidebar in the drawing tab where the user discusses the
currently displayed PHY architecture with a Circuit_Researcher-style
consultant, and the consultant may propose structured edits ("patches") the
UI applies to the diagram. This document is the CONTRACT for Graphics_Dev,
who builds the sidebar UI against it. Backend, api.js functions, and tests
are done (implementation status at the bottom); the sidebar UI is NOT built.

## Backend pieces

- `backend/arch_chat.py` - the one isolated module that knows about the
  headless `claude --agent Circuit_Researcher -p ...` chat invocation
  (read-only tool allowlist, 300 s timeout, last-8-turns history window for
  cost control), plus patch validation, reply parsing, and transcript
  persistence.
- `backend/architectures.py` - saved custom architectures under
  `<working_dir>/architectures/<name>.json` (settings.py roots pattern;
  previous working dirs stay readable).
- `backend/settings.py` - new roots: `arch_chats/`, `architectures/`.
- Endpoints in `backend/main.py`; client functions in
  `frontend/src/api.js`.

## Endpoint contract

### POST /api/arch_chat

One chat turn. SYNCHRONOUS: the response arrives only when the consultant
has answered (typically 20-90 s, up to the 300 s timeout). Each call is a
paid claude invocation.

Request body:

```json
{
  "session_id": "chat-abc123",        // client-generated, 1-64 chars [A-Za-z0-9_-]
  "message": "Add a VGA after the CTLE",
  "architecture": {                   // the diagram EXACTLY as the frontend holds it
    "blocks": [ {"id": "rx", "label": "RX CTLE", "topology": "ctle", "col": 0, "row": 0, ...}, ... ],
    "edges":  [ {"from": "rx", "to": "sampler"}, ... ]   // "connections" accepted as alias
  },
  "phy_type": "ser-des"               // optional, prompt context only
}
```

Block `topology` values are backend topology categories
(`GET /api/topologies`); `null` marks non-designable nodes (pads,
photodiode, digital cal logic). Extra block keys (`short`, `kind`, `col`,
`row`, `children`, ...) are passed through to the consultant untouched.

Success response (200):

```json
{
  "session_id": "chat-abc123",
  "reply": "<markdown text - render as markdown>",
  "patch": { "ops": [ ... ] } | null,   // null = informational answer, nothing to apply
  "patch_valid": true | false,
  "patch_errors": [ {"index": 0, "error": "..."} ],  // [] when valid or no patch
  "cost_usd": 0.05,
  "duration_ms": 41000
}
```

Errors:
- 422 - bad session_id (wrong charset / traversal), empty message, or an
  architecture that isn't `{blocks:[{id,...}], edges:[{from,to}]}`.
- 502 - the consultant invocation itself failed (timeout, CLI error). The
  `detail` string says why; NO turn is recorded in the transcript. The UI
  must surface this plainly (error bubble with the detail text) and leave
  the user's typed message in place for retry.

### Patch schema (exact)

`patch.ops` is an ordered list; ops are applied IN ORDER (an `add_block` id
may be referenced by later ops in the same patch). Each op is one of:

```json
{"op": "add_block", "id": "<new unique id>", "label": "<label>",
 "topology": "<topology value>" | null, "col": <int, optional>, "row": <int, optional>, "kind": "pad" (optional)}
{"op": "remove_block", "id": "<existing id>"}
{"op": "rename_block", "id": "<existing id>", "label": "<new label>"}
{"op": "set_topology", "id": "<existing id>", "topology": "<topology value>" | null}
{"op": "add_connection", "from": "<block id>", "to": "<block id>"}
{"op": "remove_connection", "from": "<block id>", "to": "<block id>"}
```

Server-side validation (already done - the UI does not re-validate):
- topology values checked against `topologies.ALL_TOPOLOGY_VALUES`
  (imported single source of truth) or `null`;
- referenced block ids must exist (at that point in the op sequence);
- no duplicate block ids, no dangling/duplicate/self connections;
- `remove_block` implicitly removes every edge touching that block - the
  UI MUST apply the same rule when executing the op.

Invalid ops come back with an added `"error": "<reason>"` field,
`patch_valid: false`, and `patch_errors` (index + error). A malformed
fenced-JSON patch from the model NEVER fails the chat: `patch` is `null`
and the full reply text still comes back.

### GET /api/arch_chat/{session_id}

Persisted transcript, for restoring the sidebar after a reload:

```json
{"session_id": "...", "created_at": "...", "updated_at": "...",
 "turns": [ {"role": "user", "content": "...", "ts": "..."},
            {"role": "assistant", "content": "<reply markdown>", "patch": {...}|null, "ts": "..."} ]}
```

Unknown session ids return an empty transcript (`turns: []`), not 404 -
ids are client-generated. Transcripts live at
`<working_dir>/arch_chats/<session_id>.json`; per-turn debug artifacts
(prompt + raw claude stream) at `arch_chats/<session_id>_logs/`.

### GET /api/architectures

```json
{"architectures": [ {"name": "my-serdes-v2", "label": "SerDes + VGA", "phy_type": "ser-des",
                     "created_at": "...", "updated_at": "...", "architecture": {"blocks": [...], "edges": [...]}} ]}
```

Newest first, across current + previous working dirs.
IMPORTANT MERGE NOTE: the BUILT-IN architectures are frontend-static in
`frontend/src/phyArchitectures.js` (`PHY_ARCHITECTURES`). This endpoint
returns ONLY user-saved custom ones - Graphics_Dev must merge the two in
the architecture list/selector (e.g. a "Custom" optgroup alongside the
built-in PHY types, keyed by `name`).

### POST /api/architectures

```json
{"name": "my-serdes-v2",              // 1-64 chars [A-Za-z0-9_-]; EXISTING NAME = OVERWRITE (normal re-save)
 "architecture": {"blocks": [...], "edges": [...]},
 "phy_type": "ser-des",               // optional
 "label": "SerDes + VGA"}             // optional display label, defaults to name
```

Returns the stored entry (shape as in the list). 422 on bad name or an
unusable architecture shape.

## frontend/src/api.js functions (already implemented)

- `archChat({sessionId, message, architecture, phyType})` -> POST /api/arch_chat
- `getArchChatTranscript(sessionId)` -> GET /api/arch_chat/{id}
- `saveArchitecture({name, architecture, phyType, label})` -> POST /api/architectures
- `listArchitectures()` -> GET /api/architectures

## What the sidebar UI (Graphics_Dev) must do

1. Chat panel in the drawing tab: message input + transcript rendered from
   `getArchChatTranscript()` on mount (generate/remember a session id per
   drawing session, e.g. in localStorage keyed by phy type) and appended
   from each `archChat()` response. Render `reply` as markdown.
2. Pending state: `archChat()` is slow (20-90 s) - show a spinner/typing
   indicator, disable re-submit while a call is in flight. Show the
   per-turn `cost_usd` subtly if desired.
3. Patch handling: when `patch` is non-null and `patch_valid` is true, show
   the proposed ops in human terms with Apply / Dismiss buttons; on Apply,
   execute the ops in order against the displayed architecture
   (`remove_block` also removes its edges) and re-render the diagram. When
   `patch_valid` is false, show the ops with their per-op `error` strings
   and NO apply button (the reply text still displays normally).
4. Failure handling: a 502 from `archChat()` (message in the thrown error)
   gets an inline error bubble; keep the user's message in the input for
   retry. Never render an empty assistant bubble.
5. Save/load: a "Save architecture" action calling `saveArchitecture()`
   (prompt for name/label; re-saving the same name overwrites - fine), and
   the architecture selector listing built-ins from `phyArchitectures.js`
   MERGED with `listArchitectures()` customs (see merge note above).
6. Per the project rule: drive the whole flow in a real browser before
   reporting done (a stubbed-backend chat turn is fine for UI development;
   see `backend/test_arch_chat.py` for the stub pattern).

## Implementation status (2026-08-21, Analog_Tool_Dev)

- Backend + api.js implemented as specified above. Sidebar UI NOT built
  (per plan - Graphics_Dev owns it).
- Unit/integration tests: `backend/test_arch_chat.py` - 24 passed
  (`.venv/bin/python3 -m pytest test_arch_chat.py -q`), all claude calls
  stubbed: patch validation (valid ops; bad topology; dangling/duplicate
  connections; connection-to-removed-block; unknown op; malformed patch
  object), reply parsing (patch block extracted/stripped; malformed JSON
  never fails the chat), transcript persistence across two messages
  (history windowing into the second prompt verified), consultant-failure
  502 with no fake transcript turn, session-id traversal rejection, and
  the architectures save/list/overwrite round trip.
- Live curl round trip against the running backend (port 8000):
  POST /api/architectures -> 200 stored entry; GET /api/architectures ->
  the entry with full blocks/edges; bad name -> 422. (Curl-test entry
  removed afterwards.)
- One authorized real smoke invocation: POST /api/arch_chat with a 2-block
  architecture ("add a VGA between CTLE and sampler") against the real
  claude CLI. Result: HTTP 200 in 21.5 s, cost $0.20; substantive markdown
  reply (CTLE-vs-VGA roles, AGC loop, sky130 headroom/fT realities) plus a
  VALID 4-op patch that parsed and validated cleanly:
  add_block(vga) -> remove_connection(rx->sampler) ->
  add_connection(rx->vga) -> add_connection(vga->sampler).
  Transcript correctly persisted to arch_chats/smoke-2026-08-21.json with
  the patch attached to the assistant turn; prompt + raw stream captured in
  arch_chats/smoke-2026-08-21_logs/. Smoke transcript/logs removed after
  verification. No backend swap was needed for the stubbed tests (stubbing
  is pytest monkeypatching only), so the production code path is untouched.
- Backend restarted on port 8000 with the new code; pytest + httpx added
  to backend/.venv (test-only deps).

## Implementation status (2026-08-21, Graphics_Dev): sidebar UI built

Files: NEW `frontend/src/ArchChatSidebar.jsx`; edited `App.jsx` (drawing
area wrapped in a 75/25 flex row hosting the sidebar; customArchs +
archVersion state), `phyArchitectures.js` (mutable-architecture layer),
`ArchitectureDiagram.jsx` (`version` prop forces re-read after external
edits; Reset also clears block overrides), `PhyArchitectureSelector.jsx` /
`SpecForm.jsx` (prop pass-through; PHY select now merges a "Custom (saved
architectures)" optgroup), `App.css` (sidebar styles appended).

Design notes:
- Mutable architecture state: rather than duplicating the diagram's edit
  model, chat patches/saves/loads reuse the SAME per-level localStorage
  records the diagram already edits (user blocks / hidden defaults / wire
  edits), plus one new record `arch-block-overrides-<level>` so
  rename_block / set_topology can hit built-in blocks. New helpers in
  phyArchitectures.js: `currentArchitecture()` (diagram exactly as
  displayed, for POST bodies), `applyArchOps()` (ops in order;
  remove_block purges its wires; add_block bumps a grid-cell collision to
  the next free row - the real consultant dropped its CTLE onto the
  sampler's cell), `archSnapshot()`/`restoreArchSnapshot()` (Undo of the
  last applied patch), `loadArchitectureIntoPhy()` (saved custom replaces
  the displayed top level; "Reset diagram" still restores defaults).
  Hidden-list filtering now only applies to default blocks so a loaded
  custom may reuse default ids. Block-click -> topology -> spec-form
  contract untouched (verified by test).
- Sidebar: per-phy session id persisted in localStorage
  (`arch-chat-session-<phy>`); transcript restored on mount via
  getArchChatTranscript; hand-rolled markdown (paragraphs, bullet lists,
  fenced code, inline `code`/**bold** - no new library); spinner +
  disabled send during the slow synchronous call; 502 -> inline error
  bubble, typed message kept for retry; patch proposal card in plain words
  with Apply/Dismiss (Apply only on the latest turn; older ones show
  "proposed earlier"), per-op errors + no Apply when patch_valid false;
  per-turn cost/duration shown; Save architecture (name prompt, client
  regex precheck) + Undo last patch buttons; collapsible, sticky, 25%
  width (stacks below 1100 px).

Verification (playwright/chromium on :5173 + :8000):
- Mocked /api/arch_chat (route interception, zero paid calls): 24/24
  checks - 25% layout + collapse, pending spinner/disabled send (route
  held open), markdown/code/cost rendering, valid-patch card wording,
  Apply (block + rewired edges land in the diagram and localStorage),
  Undo restore, invalid patch (per-op errors, no Apply), 502 bubble with
  detail + input preserved + no fake assistant bubble, Save against the
  REAL backend then custom optgroup merge, Reset -> load custom ->
  block-click contract. Screenshots:
  2026-08-21-archchat-{layout,collapsed,pending,patch-card,applied,
  invalid-patch,502,saved-selector,custom-loaded}.png.
- One authorized REAL invocation: 2-block custom "tiny-serdes" loaded via
  the selector, "add a CTLE before the sampler" -> 200 in 24 s, $0.13,
  substantive reply + valid 4-op patch (add ctle / disconnect rx->sampler
  / connect rx->ctle / connect ctle->sampler), applied for real;
  transcript + applied diagram survived a reload. Screenshots:
  2026-08-21-archchat-real-{before,after,restored}.png; the consultant's
  col1/row0 overlap motivated the collision fix, replay of the exact real
  patch after the fix: real-after-fixed.png (all three blocks distinct).
  Full mocked suite re-run green after the fix. Test entries
  (tiny-serdes, ui-test-serdes-ctle2, test transcript/logs) removed.
