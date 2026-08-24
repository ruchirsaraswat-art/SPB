// AFE<->digital interface workspace: the frontend's working copy of the
// interface definition (localStorage `if-def-<phyType>`, mirroring the
// diagram views' edit-record pattern), the client-side patch applier for
// if_chat ops, snapshot/undo, and the lightweight validation mirror of
// backend/interfaces.py (hard errors vs soft warnings - the server stays
// authoritative; this copy only drives the inline row annotations).
//
// Contract source: audits/2026-08-23-digital-arch-spec.md section c. The
// applier MUST mirror the server's cascade semantics (see
// interfaces.apply_if_patch): remove_signal takes its cdc entries with it;
// renames cascade into cdc/signal references; sequences are whole-list
// replacements.

import { DEFAULT_INTERFACES } from "./defaultInterfaces";

export const DIRECTIONS = ["a2d", "d2a", "ext"];
export const RATES = ["line", "word", "quasi_static", "async_event"];
export const SIGNAL_GROUPS = ["data", "clock", "control", "status", "reset", "power"];
export const HANDSHAKES = ["none", "valid", "valid_ready", "req_ack", "level", "pulse", "2ff_sync"];
export const ASYNC_DOMAIN = "async";

const NAME_RE = /^[a-z][a-z0-9_]*$/;

export function ifDefKey(phyType) {
  return `if-def-${phyType}`;
}

function deepCopy(x) {
  return JSON.parse(JSON.stringify(x));
}

export function defaultInterfaceForPhy(phyType) {
  const d = DEFAULT_INTERFACES[phyType];
  return d
    ? deepCopy(d)
    : {
        name: `${phyType}-if`,
        phy_type: phyType,
        clock_domains: [],
        signals: [],
        cdc_points: [],
        reset_sequence: [],
        power_sequence: [],
      };
}

// The working copy the editor displays and the chat sends: localStorage if
// present, else the shipped built-in default for the PHY.
export function loadIfDef(phyType) {
  try {
    const stored = JSON.parse(localStorage.getItem(ifDefKey(phyType)));
    if (stored && typeof stored === "object" && Array.isArray(stored.signals)) {
      return stored;
    }
  } catch {
    /* fall through to the default */
  }
  return defaultInterfaceForPhy(phyType);
}

export function saveIfDef(phyType, def) {
  try {
    localStorage.setItem(ifDefKey(phyType), JSON.stringify(def));
  } catch {
    /* localStorage unavailable - the edit just won't persist */
  }
}

export function resetIfDef(phyType) {
  try {
    localStorage.removeItem(ifDefKey(phyType));
  } catch {
    /* ignore */
  }
}

// Snapshot/restore for Undo of an applied chat patch (same shape idea as
// archSnapshot: the raw stored string, null = "was at built-in default").
export function ifSnapshot(phyType) {
  const k = ifDefKey(phyType);
  try {
    return { [k]: localStorage.getItem(k) };
  } catch {
    return { [k]: null };
  }
}

export function restoreIfSnapshot(phyType, snap) {
  const k = ifDefKey(phyType);
  try {
    if (snap[k] == null) localStorage.removeItem(k);
    else localStorage.setItem(k, snap[k]);
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// Patch applier: executes server-validated ops in order against the stored
// working copy. Ids/enums were validated server-side, so this just executes
// (exactly like applyArchOps) - but the cascades MUST match the server's.

export function applyIfOps(phyType, ops) {
  const def = loadIfDef(phyType);
  for (const op of ops || []) {
    if (op.error) continue; // never execute an op the server flagged
    switch (op.op) {
      case "add_signal":
        def.signals = [...def.signals, { ...op.signal }];
        break;
      case "remove_signal":
        def.signals = def.signals.filter((s) => s.name !== op.name);
        def.cdc_points = def.cdc_points.filter((c) => c.signal !== op.name);
        break;
      case "update_signal": {
        const idx = def.signals.findIndex((s) => s.name === op.name);
        if (idx < 0) break;
        const merged = { ...def.signals[idx], ...op.fields };
        def.signals[idx] = merged;
        if (merged.name !== op.name) {
          def.cdc_points = def.cdc_points.map((c) =>
            c.signal === op.name ? { ...c, signal: merged.name } : c
          );
        }
        break;
      }
      case "add_clock_domain":
        def.clock_domains = [...def.clock_domains, { ...op.domain }];
        break;
      case "remove_clock_domain":
        def.clock_domains = def.clock_domains.filter((d) => d.name !== op.name);
        break;
      case "update_clock_domain": {
        const idx = def.clock_domains.findIndex((d) => d.name === op.name);
        if (idx < 0) break;
        const merged = { ...def.clock_domains[idx], ...op.fields };
        def.clock_domains[idx] = merged;
        if (merged.name !== op.name) {
          def.signals = def.signals.map((s) =>
            s.clock_domain === op.name ? { ...s, clock_domain: merged.name } : s
          );
          def.cdc_points = def.cdc_points.map((c) => ({
            ...c,
            from_domain: c.from_domain === op.name ? merged.name : c.from_domain,
            to_domain: c.to_domain === op.name ? merged.name : c.to_domain,
          }));
        }
        break;
      }
      case "add_cdc":
        def.cdc_points = [...def.cdc_points, { ...op.cdc }];
        break;
      case "remove_cdc":
        def.cdc_points = def.cdc_points.filter((c) => c.signal !== op.signal);
        break;
      case "set_reset_sequence":
        def.reset_sequence = (op.steps || []).map((s) => ({ ...s }));
        break;
      case "set_power_sequence":
        def.power_sequence = (op.steps || []).map((s) => ({ ...s }));
        break;
      default:
        break;
    }
  }
  saveIfDef(phyType, def);
  return def;
}

// ---------------------------------------------------------------------------
// Lightweight validation mirror (per-row annotations for the editor). Same
// rule set as interfaces.validate_interface; the server re-checks on save.

export function validateIfDef(def) {
  const domainNames = new Set();
  const domainIssues = (def.clock_domains || []).map((dom) => {
    const errors = [];
    if (typeof dom.name !== "string" || !NAME_RE.test(dom.name)) {
      errors.push(`Invalid domain name "${dom.name ?? ""}" (must match [a-z][a-z0-9_]*)`);
    } else if (dom.name === ASYNC_DOMAIN) {
      errors.push(`"async" is reserved (it means "no clock domain")`);
    } else if (domainNames.has(dom.name)) {
      errors.push(`Duplicate domain name "${dom.name}"`);
    } else {
      domainNames.add(dom.name);
    }
    return { errors, warnings: [] };
  });

  const signalNames = new Set();
  const cdcSignals = new Set((def.cdc_points || []).map((c) => c.signal));
  const signalIssues = (def.signals || []).map((sig) => {
    const errors = [];
    const warnings = [];
    if (typeof sig.name !== "string" || !NAME_RE.test(sig.name)) {
      errors.push(`Invalid signal name "${sig.name ?? ""}" (must match [a-z][a-z0-9_]*)`);
    } else if (signalNames.has(sig.name)) {
      errors.push(`Duplicate signal name "${sig.name}"`);
    } else {
      signalNames.add(sig.name);
    }
    if (!DIRECTIONS.includes(sig.direction)) errors.push(`Unknown direction "${sig.direction}"`);
    if (!RATES.includes(sig.rate)) errors.push(`Unknown rate "${sig.rate}"`);
    if (!SIGNAL_GROUPS.includes(sig.group)) errors.push(`Unknown group "${sig.group}"`);
    if (!HANDSHAKES.includes(sig.handshake)) errors.push(`Unknown handshake "${sig.handshake}"`);
    const w = Number(sig.width);
    if (!Number.isInteger(w) || w < 1) errors.push(`Width must be an integer >= 1`);
    if (sig.clock_domain !== ASYNC_DOMAIN && !domainNames.has(sig.clock_domain)) {
      errors.push(`Unknown clock domain "${sig.clock_domain}"`);
    }
    if (sig.rate === "word") {
      const mhz = Number(sig.rate_mhz);
      if (!(mhz > 0)) errors.push(`rate_mhz is required when rate is "word"`);
      else if (mhz > 200) warnings.push(`Above ~200 MHz is aggressive for sky130 synthesized logic.`);
    }
    if (sig.rate === "line") {
      warnings.push(
        "Line-rate signal crossing the boundary - only word-rate signals should normally cross (that is what the deserializer is for)."
      );
    }
    if (
      sig.clock_domain === ASYNC_DOMAIN &&
      !cdcSignals.has(sig.name) &&
      sig.handshake !== "2ff_sync"
    ) {
      warnings.push("Async signal with no CDC entry and handshake is not 2ff_sync - document the synchronization plan.");
    }
    if (sig.handshake === "pulse" && !cdcSignals.has(sig.name)) {
      warnings.push("Pulse with no CDC entry - a single-cycle event must be stretched/synced if it crosses domains.");
    }
    return { errors, warnings };
  });

  const cdcIssues = (def.cdc_points || []).map((cdc) => {
    const errors = [];
    if (!signalNames.has(cdc.signal)) errors.push(`References nonexistent signal "${cdc.signal}"`);
    for (const key of ["from_domain", "to_domain"]) {
      const d = cdc[key];
      if (d !== ASYNC_DOMAIN && !domainNames.has(d)) {
        errors.push(`${key} "${d}" is not "async" or a defined domain`);
      }
    }
    return { errors, warnings: [] };
  });

  const sequenceIssues = {};
  for (const key of ["reset_sequence", "power_sequence"]) {
    sequenceIssues[key] = (def[key] || []).map((step) => {
      const errors = [];
      if (typeof step.action !== "string" || !step.action.trim()) {
        errors.push("Every step needs a non-empty action");
      }
      return { errors, warnings: [] };
    });
  }

  const allErrors = [
    ...domainIssues.flatMap((i) => i.errors),
    ...signalIssues.flatMap((i) => i.errors),
    ...cdcIssues.flatMap((i) => i.errors),
    ...sequenceIssues.reset_sequence.flatMap((i) => i.errors),
    ...sequenceIssues.power_sequence.flatMap((i) => i.errors),
  ];
  const allWarnings = [
    ...signalIssues.flatMap((i) => i.warnings),
  ];

  return { domainIssues, signalIssues, cdcIssues, sequenceIssues, allErrors, allWarnings };
}
