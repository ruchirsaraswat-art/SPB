import { useEffect, useMemo, useState } from "react";
import ArchitectureDiagram from "./ArchitectureDiagram";
import { firmwareArchStore, FIRMWARE_DESCRIPTIONS } from "./firmwareArchitectures";
import { getFirmwareBlocks, fwGenerate } from "./api";
import { loadIfDef } from "./interfaceStore";

// Firmware workspace panel (2026-08-23 firmware-section spec, section d):
// the firmware stack diagram (same wired ArchitectureDiagram, backed by the
// "fw-arch-" store and the FIRMWARE block catalog) plus the one v1 codegen
// action - "Generate register layer" - which turns the stored interface
// definition into compiled+tested C via a paid headless Firmware_Coder run.
// Differences from the AFE view (variant="firmware"):
//   - NO model checkboxes anywhere (the firmware catalog has no "models"
//     key - firmware is host-compiled C, not Verilog);
//   - block selection is VISUAL ONLY (nothing feeds the spec form or any
//     build flow);
//   - the `host-if`/`csr-if` kind:"iface" anchors are the block's two
//     contracts (host API surface / controller CSRs per the interface).
// `version` bumps whenever the sidebar chat applied/undid a firmware patch
// through firmwareArchStore - the diagram re-reads the store on change.

function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

export default function FirmwarePanel({ phyType, version }) {
  const [catalog, setCatalog] = useState(null); // {groups, by_phy, note} or null
  const [selectedBlock, setSelectedBlock] = useState(null);
  // Generate-action state: idle -> confirming -> running -> a result/error.
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null); // fwGenerate response, or null
  const [genError, setGenError] = useState(null);
  const [showOutput, setShowOutput] = useState(false);

  useEffect(() => {
    let alive = true;
    getFirmwareBlocks()
      .then((c) => {
        if (alive) setCatalog(c);
      })
      .catch(() => {
        // catalog unavailable - the diagram still renders; only the
        // "Add block" picker stays empty until the backend is reachable
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setSelectedBlock(null);
    setConfirming(false);
    setResult(null);
    setGenError(null);
    setShowOutput(false);
  }, [phyType]);

  // Catalog groups -> picker optgroups; within each group the blocks the
  // firmware catalog lists as typical for this PHY sort first (the rest stay
  // offered - the server only soft-warns on off-per-PHY choices).
  const topologyGroups = useMemo(() => {
    if (!catalog) return [];
    const typical = new Set(catalog.by_phy?.[phyType] || []);
    return (catalog.groups || []).map((g) => ({
      group: g.group,
      options: [...g.options].sort(
        (a, b) => Number(typical.has(b.value)) - Number(typical.has(a.value))
      ),
    }));
  }, [catalog, phyType]);

  const ifDef = loadIfDef(phyType); // re-read every render: chat/editor may have changed it

  async function handleGenerate() {
    setConfirming(false);
    setRunning(true);
    setGenError(null);
    setResult(null);
    setShowOutput(false);
    try {
      const r = await fwGenerate({ phyType, interfaceDef: loadIfDef(phyType) });
      setResult(r);
      setShowOutput(r.status !== "pass"); // surface the log tail on failure
    } catch (e) {
      setGenError(errText(e));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="firmware-panel">
      <p className="hint">{FIRMWARE_DESCRIPTIONS[phyType] || "Firmware stack"}</p>
      <ArchitectureDiagram
        phyType={phyType}
        version={version}
        store={firmwareArchStore}
        variant="firmware"
        selectedBlock={selectedBlock}
        onSelectBlock={(b) => setSelectedBlock((cur) => (cur === b.id ? null : b.id))}
        topologyGroups={topologyGroups}
      />

      <div className="fw-generate">
        <div className="fw-generate-row">
          <button
            type="button"
            className="fw-generate-btn"
            onClick={() => setConfirming(true)}
            disabled={running || confirming}
            title="Generate the register-layer C code (regs header, driver, stub register model, host tests, Makefile) from the stored interface definition"
          >
            {running ? "Generating..." : "Generate register layer"}
          </button>
          <span className="hint">
            One paid Firmware_Coder run (expect a few $ and several minutes). Generates
            regs header + driver + stub + host tests + Makefile from the{" "}
            <code>{ifDef?.name || `${phyType} interface`}</code> definition into{" "}
            <code>firmware/{phyType}/</code>; the tool re-runs{" "}
            <code>gcc -std=c99 -Wall -Werror</code> + the unit tests itself and only
            reports pass on exit 0.
          </span>
        </div>

        {confirming && (
          <div className="fw-generate-confirm">
            <span>
              Start the paid generation run now? It blocks this panel for several minutes.
            </span>
            <button type="button" onClick={handleGenerate}>
              Start run
            </button>
            <button type="button" className="toggle-raw" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        )}

        {running && (
          <div className="fw-generate-pending">
            <span className="spinner" />
            Firmware_Coder is generating + testing the register layer (typically 2-10 min)...
          </div>
        )}

        {genError && <div className="error-box fw-generate-error">{genError}</div>}

        {result && (
          <div className={`fw-result ${result.status === "pass" ? "pass" : "fail"}`}>
            <div className="fw-result-head">
              <span className={`fw-badge ${result.status === "pass" ? "pass" : "fail"}`}>
                {result.status === "pass" ? "PASS" : "FAIL"}
              </span>
              <span className={`fw-badge sub ${result.compile_ok ? "pass" : "fail"}`}>
                compile (gcc -std=c99 -Wall -Werror): {result.compile_ok ? "ok" : "failed"}
              </span>
              <span className={`fw-badge sub ${result.tests_ok ? "pass" : "fail"}`}>
                unit tests: {result.tests_ok ? "ok" : "failed"}
              </span>
              <span className="fw-result-meta">
                {result.cost_usd != null ? `$${Number(result.cost_usd).toFixed(2)}` : ""}
                {result.duration_s != null ? ` · ${result.duration_s} s` : ""}
              </span>
            </div>
            {result.status !== "pass" && result.reason && (
              <p className="fw-result-reason">{result.reason}</p>
            )}
            {result.files?.length > 0 && (
              <ul className="fw-file-list">
                {result.files.map((f) => (
                  <li key={f}>
                    <code>{f}</code>
                  </li>
                ))}
              </ul>
            )}
            {result.missing_files?.length > 0 && (
              <p className="fw-result-reason">
                Missing deliverables: {result.missing_files.join(", ")}
              </p>
            )}
            {result.test_output && (
              <>
                <button
                  type="button"
                  className="toggle-raw"
                  onClick={() => setShowOutput((s) => !s)}
                >
                  {showOutput ? "Hide compile/test log" : "Show compile/test log"}
                </button>
                {showOutput && <pre className="fw-test-output">{result.test_output}</pre>}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
