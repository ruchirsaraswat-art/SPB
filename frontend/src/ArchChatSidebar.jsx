import { useEffect, useRef, useState } from "react";
import { archChat, getArchChatTranscript, saveArchitecture, ifChat, getIfChatTranscript, fwChat, getFwChatTranscript } from "./api";
import { afeArchStore } from "./phyArchitectures";
import { digitalArchStore } from "./digitalArchitectures";
import { firmwareArchStore } from "./firmwareArchitectures";
import { applyIfOps, ifSnapshot, restoreIfSnapshot, loadIfDef } from "./interfaceStore";

// Context-switching chat sidebar (2026-08-23 digital-arch spec, section d).
// ONE sidebar follows the focused workspace panel:
//   - workspace "afe"       -> POST /api/arch_chat (view "afe"), patches apply
//                              through the "arch-" store (pre-existing flow;
//                              full contract: audits/2026-08-21-arch-chat-feature.md)
//   - workspace "digital"   -> POST /api/arch_chat (view "digital"), AFE
//                              architecture sent as read-only context, patches
//                              apply through the "dig-arch-" store
//   - workspace "interface" -> POST /api/if_chat with the interface definition
//                              + both architectures (read-only), patches apply
//                              through the if-def working copy (interfaceStore)
// Three separate session-id records (arch-chat-session-<phy>,
// dig-chat-session-<phy>, if-chat-session-<phy>) keep transcripts coherent
// per workspace.
//
// The chat call is SYNCHRONOUS and slow (typically 20-90 s, each turn a paid
// claude invocation) - one in-flight turn at a time, spinner meanwhile. A
// failed turn (502: timeout/CLI error) shows an inline error bubble and
// keeps the typed message in the input for retry.

const WORKSPACE_META = {
  afe: {
    title: "AFE architecture chat",
    sessionPrefix: "arch-chat-session",
    idPrefix: "chat",
    hint: (phy) =>
      `Discuss the displayed ${phy} AFE architecture with the consultant - it can propose diagram edits you Apply or Dismiss.`,
    emptyHint: 'No messages yet - ask something like "add a CTLE before the sampler".',
  },
  // "Controller" is the display name for the digital side (user request,
  // 2026-08-23) - session prefixes / API view stay "digital".
  digital: {
    title: "Controller architecture chat",
    sessionPrefix: "dig-chat-session",
    idPrefix: "digchat",
    hint: (phy) =>
      `Discuss the ${phy} controller (digital) microarchitecture - aligners, FIFOs, CSR, training FSMs. The AFE diagram goes along as read-only context.`,
    emptyHint: 'No messages yet - ask something like "do I need a scrambler in this datapath?".',
  },
  interface: {
    title: "Interface chat",
    sessionPrefix: "if-chat-session",
    idPrefix: "ifchat",
    hint: (phy) =>
      `Discuss the ${phy} AFE-controller interface table (signals, clock domains, CDC, sequences). Both diagrams are read-only context; patches edit the table.`,
    emptyHint: 'No messages yet - ask something like "is the CDC plan for the status signals complete?".',
  },
  // Firmware workspace (2026-08-23 firmware-section spec): Firmware_Coder
  // persona, the stored interface definition rides along READ-ONLY, patches
  // edit the firmware diagram through the "fw-arch-" store.
  firmware: {
    title: "Firmware chat",
    sessionPrefix: "fw-chat-session",
    idPrefix: "fwchat",
    hint: (phy) =>
      `Discuss the ${phy} firmware stack (boot/init, supervisors, driver layering, host API) with Firmware_Coder. The interface definition goes along as read-only context; patches edit the firmware diagram.`,
    emptyHint: 'No messages yet - ask something like "should ZQ recal run from the IRQ handler or a timer poll?".',
  },
};

function sessionIdFor(workspace, phyType) {
  const meta = WORKSPACE_META[workspace];
  const key = `${meta.sessionPrefix}-${phyType}`;
  try {
    const existing = localStorage.getItem(key);
    if (existing && /^[A-Za-z0-9_-]{1,64}$/.test(existing)) return existing;
    const id = `${meta.idPrefix}-${phyType}-${Math.random().toString(36).slice(2, 10)}`.replace(/[^A-Za-z0-9_-]/g, "-");
    localStorage.setItem(key, id);
    return id;
  } catch {
    return `${meta.idPrefix}-${Date.now()}`;
  }
}

function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

// --- Minimal markdown rendering (no library - fenced code blocks,
// paragraphs, bullet lists, inline `code` / **bold**). ---

function renderInline(s) {
  return s.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((t, i) => {
    if (t.startsWith("`") && t.endsWith("`")) return <code key={i}>{t.slice(1, -1)}</code>;
    if (t.startsWith("**") && t.endsWith("**")) return <strong key={i}>{t.slice(2, -2)}</strong>;
    return t;
  });
}

function renderProse(text, keyBase) {
  return text
    .split(/\n{2,}/)
    .filter((b) => b.trim())
    .map((block, j) => {
      const lines = block.split("\n").filter((l) => l.trim());
      if (lines.length > 0 && lines.every((l) => /^\s*([-*]|\d+\.)\s/.test(l))) {
        return (
          <ul key={`${keyBase}-${j}`}>
            {lines.map((l, k) => (
              <li key={k}>{renderInline(l.replace(/^\s*([-*]|\d+\.)\s/, ""))}</li>
            ))}
          </ul>
        );
      }
      if (/^#{1,6}\s/.test(block)) {
        return (
          <p key={`${keyBase}-${j}`} className="chat-heading">
            {renderInline(block.replace(/^#{1,6}\s/, ""))}
          </p>
        );
      }
      return <p key={`${keyBase}-${j}`}>{renderInline(block)}</p>;
    });
}

function renderMarkdown(text) {
  const parts = String(text ?? "").split(/```[^\n]*\n?/);
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <pre key={i} className="chat-code">
        <code>{part.replace(/\n$/, "")}</code>
      </pre>
    ) : (
      <span key={i}>{renderProse(part, i)}</span>
    )
  );
}

// One patch op in plain words for the proposal card - covers both the
// diagram op set and the interface op set.
function opSummary(op) {
  switch (op.op) {
    case "add_block":
      return `Add block "${op.label || op.id}" (${op.topology || "not designable"})`;
    case "remove_block":
      return `Remove block "${op.id}" (and its wires)`;
    case "rename_block":
      return `Rename "${op.id}" to "${op.label}"`;
    case "set_topology":
      return `Set "${op.id}" topology to ${op.topology || "none"}`;
    case "add_connection":
      return `Connect ${op.from} → ${op.to}`;
    case "remove_connection":
      return `Disconnect ${op.from} → ${op.to}`;
    case "add_signal":
      return `Add signal "${op.signal?.name}" (${op.signal?.direction}, ${op.signal?.group})`;
    case "remove_signal":
      return `Remove signal "${op.name}" (and its CDC entries)`;
    case "update_signal":
      return `Update signal "${op.name}" (${Object.keys(op.fields || {}).join(", ")})`;
    case "add_clock_domain":
      return `Add clock domain "${op.domain?.name}"`;
    case "remove_clock_domain":
      return `Remove clock domain "${op.name}"`;
    case "update_clock_domain":
      return `Update clock domain "${op.name}" (${Object.keys(op.fields || {}).join(", ")})`;
    case "add_cdc":
      return `Add CDC entry for "${op.cdc?.signal}" (${op.cdc?.from_domain} → ${op.cdc?.to_domain}, ${op.cdc?.strategy})`;
    case "remove_cdc":
      return `Remove CDC entry for "${op.signal}"`;
    case "set_reset_sequence":
      return `Replace the reset sequence (${(op.steps || []).length} steps)`;
    case "set_power_sequence":
      return `Replace the power sequence (${(op.steps || []).length} steps)`;
    default:
      return JSON.stringify(op);
  }
}

export default function ArchChatSidebar({ phyType, workspace = "afe", onArchEdited, onSaved, onInterfaceEdited }) {
  const [collapsed, setCollapsed] = useState(false);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [transcriptError, setTranscriptError] = useState(null);
  // Which assistant message's patch card was applied/dismissed (index ->
  // "applied" | "dismissed"); session-local only.
  const [patchStates, setPatchStates] = useState({});
  // Undo for the LAST applied patch: pre-apply snapshot of the target
  // workspace's records (diagram edit records, or the if-def working copy).
  const [undoInfo, setUndoInfo] = useState(null);
  const [saveStatus, setSaveStatus] = useState(null); // {kind: "info"|"error", text}
  const scrollRef = useRef(null);
  const meta = WORKSPACE_META[workspace] || WORKSPACE_META.afe;
  const sessionId = sessionIdFor(workspace, phyType);

  // Restore the persisted transcript when the sidebar mounts / the PHY or
  // focused workspace (and with it the per-workspace session id) changes.
  useEffect(() => {
    let stale = false;
    setMessages([]);
    setPatchStates({});
    setUndoInfo(null);
    setSaveStatus(null);
    setTranscriptError(null);
    const fetchTranscript =
      workspace === "interface"
        ? getIfChatTranscript
        : workspace === "firmware"
        ? getFwChatTranscript
        : getArchChatTranscript;
    fetchTranscript(sessionId)
      .then((t) => {
        if (stale) return;
        setMessages(
          (t.turns || []).map((turn) => ({
            role: turn.role,
            content: turn.content,
            patch: turn.patch || null,
            // stored patches carry per-op "error" fields when invalid
            patchValid: turn.patch ? !(turn.patch.ops || []).some((o) => o.error) : true,
            patchErrors: [],
          }))
        );
      })
      .catch((e) => {
        if (!stale) setTranscriptError(errText(e));
      });
    return () => {
      stale = true;
    };
  }, [sessionId, workspace]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending]);

  async function sendTurn(msg) {
    if (workspace === "interface") {
      return ifChat({
        sessionId,
        message: msg,
        interfaceDef: loadIfDef(phyType),
        afeArchitecture: afeArchStore.currentArchitecture(phyType),
        digitalArchitecture: digitalArchStore.currentArchitecture(phyType),
        phyType,
      });
    }
    if (workspace === "firmware") {
      return fwChat({
        sessionId,
        message: msg,
        firmwareArchitecture: firmwareArchStore.currentArchitecture(phyType),
        interfaceDef: loadIfDef(phyType),
        phyType,
      });
    }
    if (workspace === "digital") {
      return archChat({
        sessionId,
        message: msg,
        architecture: digitalArchStore.currentArchitecture(phyType),
        phyType,
        view: "digital",
        afeArchitecture: afeArchStore.currentArchitecture(phyType),
      });
    }
    return archChat({
      sessionId,
      message: msg,
      architecture: afeArchStore.currentArchitecture(phyType),
      phyType,
      view: "afe",
    });
  }

  async function handleSend() {
    const msg = input.trim();
    if (!msg || pending) return;
    setPending(true);
    setSaveStatus(null);
    // optimistic user bubble; removed again if the call fails (the typed
    // message stays in the input for retry only on failure)
    setMessages((m) => [...m, { role: "user", content: msg }]);
    try {
      const r = await sendTurn(msg);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: r.reply,
          patch: r.patch,
          patchValid: r.patch_valid !== false,
          patchErrors: r.patch_errors || [],
          patchWarnings: r.patch_warnings || [],
          costUsd: r.cost_usd,
          durationMs: r.duration_ms,
        },
      ]);
      setInput("");
    } catch (e) {
      setMessages((m) => [...m.slice(0, -1), { role: "error", content: errText(e) }]);
    } finally {
      setPending(false);
    }
  }

  function handleApply(idx) {
    const msg = messages[idx];
    if (!msg?.patch?.ops) return;
    if (workspace === "interface") {
      const snapshot = ifSnapshot(phyType);
      applyIfOps(phyType, msg.patch.ops);
      setUndoInfo({ snapshot, phyType, workspace });
      onInterfaceEdited?.();
    } else {
      const store =
        workspace === "digital" ? digitalArchStore : workspace === "firmware" ? firmwareArchStore : afeArchStore;
      const snapshot = store.archSnapshot(phyType);
      store.applyArchOps(phyType, msg.patch.ops);
      setUndoInfo({ snapshot, phyType, workspace });
      onArchEdited?.(workspace);
    }
    setPatchStates((s) => ({ ...s, [idx]: "applied" }));
  }

  function handleDismiss(idx) {
    setPatchStates((s) => ({ ...s, [idx]: "dismissed" }));
  }

  function handleUndo() {
    if (!undoInfo) return;
    if (undoInfo.workspace === "interface") {
      restoreIfSnapshot(undoInfo.phyType, undoInfo.snapshot);
      onInterfaceEdited?.();
    } else {
      const store =
        undoInfo.workspace === "digital"
          ? digitalArchStore
          : undoInfo.workspace === "firmware"
          ? firmwareArchStore
          : afeArchStore;
      store.restoreArchSnapshot(undoInfo.phyType, undoInfo.snapshot);
      onArchEdited?.(undoInfo.workspace);
    }
    setUndoInfo(null);
    setPatchStates((s) => {
      const next = { ...s };
      for (const k of Object.keys(next)) if (next[k] === "applied") delete next[k];
      return next;
    });
  }

  async function handleSave() {
    const name = window.prompt(
      "Save the displayed architecture as (1-64 chars: letters, digits, - _; an existing name is overwritten):",
      `${phyType}-custom`
    );
    if (name == null) return;
    if (!/^[A-Za-z0-9_-]{1,64}$/.test(name)) {
      setSaveStatus({ kind: "error", text: "Invalid name: use 1-64 letters, digits, - or _ only." });
      return;
    }
    try {
      const entry = await saveArchitecture({
        name,
        architecture: afeArchStore.currentArchitecture(phyType),
        phyType,
        label: name,
      });
      setSaveStatus({ kind: "info", text: `Saved as "${entry.name}" - it's now in the architecture selector.` });
      onSaved?.();
    } catch (e) {
      setSaveStatus({ kind: "error", text: errText(e) });
    }
  }

  const lastAssistantIdx = messages.reduce((acc, m, i) => (m.role === "assistant" ? i : acc), -1);

  return (
    <aside className={`arch-chat-panel ${collapsed ? "collapsed" : ""}`}>
      <div className="arch-chat-head">
        {!collapsed && <h3>{meta.title}</h3>}
        <button
          type="button"
          className="toggle-raw collapse-btn"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand the consultant chat" : "Collapse the chat sidebar"}
        >
          {collapsed ? "Chat" : "Collapse"}
        </button>
      </div>

      {!collapsed && (
        <>
          <p className="hint arch-chat-hint">
            {meta.hint(phyType)} Each turn is a real agent run (20-90 s).
          </p>

          <div className="arch-chat-transcript" ref={scrollRef}>
            {transcriptError && <div className="chat-bubble error">Could not restore the chat history: {transcriptError}</div>}
            {messages.length === 0 && !pending && !transcriptError && (
              <p className="chat-empty">{meta.emptyHint}</p>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`chat-bubble ${m.role}`}>
                {m.role === "assistant" ? renderMarkdown(m.content) : <p>{m.content}</p>}
                {m.role === "assistant" && m.costUsd != null && (
                  <div className="chat-meta">
                    ${Number(m.costUsd).toFixed(2)}
                    {m.durationMs != null ? ` · ${Math.round(m.durationMs / 1000)} s` : ""}
                  </div>
                )}
                {m.role === "assistant" && m.patch?.ops?.length > 0 && (
                  <div className={`chat-patch-card ${m.patchValid ? "" : "invalid"}`}>
                    <strong>
                      {m.patchValid
                        ? workspace === "interface"
                          ? "Proposed interface edit"
                          : "Proposed diagram edit"
                        : "Proposed edit (invalid - cannot apply)"}
                    </strong>
                    <ul className="chat-patch-ops">
                      {m.patch.ops.map((op, k) => (
                        <li key={k}>
                          {opSummary(op)}
                          {op.error && <span className="op-error"> — {op.error}</span>}
                          {op.warning && <span className="op-warning"> — {op.warning}</span>}
                        </li>
                      ))}
                    </ul>
                    {m.patchValid ? (
                      patchStates[i] === "applied" ? (
                        <p className="chat-patch-state">
                          {workspace === "interface" ? "Applied to the interface table." : "Applied to the diagram."}
                        </p>
                      ) : patchStates[i] === "dismissed" ? (
                        <p className="chat-patch-state">Dismissed.</p>
                      ) : i === lastAssistantIdx ? (
                        <div className="chat-patch-actions">
                          <button type="button" onClick={() => handleApply(i)} disabled={pending}>
                            Apply
                          </button>
                          <button type="button" className="toggle-raw" onClick={() => handleDismiss(i)}>
                            Dismiss
                          </button>
                        </div>
                      ) : (
                        <p className="chat-patch-state">Proposed earlier in this session.</p>
                      )
                    ) : (
                      <p className="chat-patch-state">The reply above still applies - only the structured edit was rejected.</p>
                    )}
                  </div>
                )}
              </div>
            ))}
            {pending && (
              <div className="chat-pending">
                <span className="spinner" />
                Consultant is thinking (20-90 s)...
              </div>
            )}
          </div>

          <div className="arch-chat-input">
            <textarea
              rows={2}
              placeholder={
                workspace === "interface"
                  ? "Ask about or edit the interface table..."
                  : workspace === "digital"
                  ? "Ask about or edit the digital architecture..."
                  : workspace === "firmware"
                  ? "Ask about or edit the firmware stack..."
                  : "Ask about or edit this architecture..."
              }
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              disabled={pending}
            />
            <button type="button" onClick={handleSend} disabled={pending || !input.trim()}>
              {pending ? "..." : "Send"}
            </button>
          </div>

          <div className="arch-chat-actions">
            {workspace === "afe" && (
              <button type="button" className="toggle-raw" onClick={handleSave} title="Save the displayed architecture under a name (POST /api/architectures)">
                Save architecture
              </button>
            )}
            {undoInfo && (
              <button type="button" className="toggle-raw" onClick={handleUndo} title="Revert the last applied patch">
                Undo last patch
              </button>
            )}
          </div>
          {saveStatus && (
            <p className={saveStatus.kind === "error" ? "field-warning" : "hint"}>{saveStatus.text}</p>
          )}
        </>
      )}
    </aside>
  );
}
