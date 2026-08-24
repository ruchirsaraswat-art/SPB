import { useCallback, useEffect, useMemo, useState } from "react";
import ArchitectureDiagram from "./ArchitectureDiagram";
import SpecDocsPanel from "./SpecDocsPanel";
import { BlockRtlMenu, ControllerRtlLauncher } from "./RtlGenerateMenu";
import { digitalArchStore, DIGITAL_DESCRIPTIONS } from "./digitalArchitectures";
import { getDigitalBlocks, listLibraries, listSpecDocs } from "./api";
import { loadIfDef } from "./interfaceStore";

// Digital-architecture (Controller) workspace panel: the same wired
// ArchitectureDiagram as the AFE view, backed by the "dig-arch-" store and
// the DIGITAL block catalog (GET /api/digital_blocks) for the "Add block"
// picker. Differences from the AFE view (variant="digital"):
//   - teal block accent + indigo `kind:"iface"` boundary anchors, so the two
//     views are instantly tellable apart;
//   - no build-status captions/legend (digital blocks aren't Circuit_Builder
//     builds).
// 2026-08-23 rtl-gen spec additions:
//   - spec-document intake: the compact banner on first visit + the
//     "Spec docs (N)" toolbar button opening the manager (SpecDocsPanel);
//   - selecting a designable block opens the "Block RTL" menu under the
//     diagram (catalog-fields mini-form persisted in dig-fields-<phy>,
//     library picker, spec-doc chips, cost-confirmed generate);
//   - the "Generate controller RTL" toolbar button with the explicit
//     most-expensive-run confirmation card.
// `version` bumps whenever the sidebar chat applied/undid a digital patch
// through digitalArchStore - the diagram re-reads the store on change.

export default function DigitalArchPanel({ phyType, version, onRtlSubmit, rtlSubmitting }) {
  const [catalog, setCatalog] = useState(null); // {groups, by_phy} or null
  const [selectedBlock, setSelectedBlock] = useState(null);
  const [libraries, setLibraries] = useState([]);
  const [specData, setSpecData] = useState(null); // {docs, status} for this phy
  const [specOpen, setSpecOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    getDigitalBlocks()
      .then((c) => {
        if (alive) setCatalog(c);
      })
      .catch(() => {
        // catalog unavailable - the diagram still renders; only the
        // "Add block" picker stays empty until the backend is reachable
      });
    listLibraries()
      .then((d) => {
        if (alive) setLibraries(d.libraries || []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const refreshSpecDocs = useCallback(() => {
    listSpecDocs(phyType)
      .then(setSpecData)
      .catch(() => setSpecData(null));
  }, [phyType]);

  useEffect(() => {
    refreshSpecDocs();
  }, [refreshSpecDocs]);

  useEffect(() => {
    setSelectedBlock(null);
    setSpecOpen(false);
  }, [phyType]);

  // Catalog groups -> picker optgroups; within each group the blocks the
  // digital catalog lists as typical for this PHY sort first (the rest stay
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

  const catalogByValue = useMemo(() => {
    const out = {};
    for (const g of catalog?.groups || []) {
      for (const o of g.options) out[o.value] = o;
    }
    return out;
  }, [catalog]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const architecture = useMemo(
    () => digitalArchStore.currentArchitecture(phyType),
    [phyType, version]
  );

  const docs = specData?.docs || [];
  const status = specData?.status?.[phyType] || { answer: null };
  // Docs whose applies_to_workspaces includes the controller workspace are
  // what RTL runs and the block menu chips carry.
  const controllerDocs = docs.filter((d) =>
    (d.applies_to_workspaces || []).includes("controller")
  );

  const selected = architecture.blocks.find((b) => b.id === selectedBlock);
  const selectedDesignable = selected && selected.topology ? selected : null;

  // Assemble the full rtl_generate request from the partial the menus emit
  // (scope/block_id/library/cell/spec_doc_ids) + the live diagram/interface.
  const handleRtlSubmit = useCallback(
    (partial) => {
      const arch = digitalArchStore.currentArchitecture(phyType);
      const digFields = JSON.parse(localStorage.getItem(`dig-fields-${phyType}`) || "{}");
      onRtlSubmit?.({
        ...partial,
        phy_type: phyType,
        digital_architecture: {
          blocks: arch.blocks.map((b) => ({
            id: b.id,
            label: b.label || b.short || b.id,
            topology: b.topology || null,
            kind: b.kind,
          })),
          edges: (arch.edges || []).map((e) => ({ from: e.from, to: e.to })),
        },
        block_fields: digFields,
        interface: loadIfDef(phyType),
      });
    },
    [phyType, onRtlSubmit]
  );

  return (
    <div className="digital-arch-panel">
      <div className="rtl-toolbar">
        <p className="hint">{DIGITAL_DESCRIPTIONS[phyType] || "Digital microarchitecture"}</p>
        <span className="rtl-toolbar-actions">
          <button
            type="button"
            className="toggle-raw spec-docs-btn"
            onClick={() => setSpecOpen((v) => !v)}
            title="Specification documents attached to this controller (requirements source)"
          >
            Spec docs ({docs.length})
          </button>
          <ControllerRtlLauncher
            phyType={phyType}
            architecture={architecture}
            catalogByValue={catalogByValue}
            libraries={libraries}
            specDocs={controllerDocs}
            ifDef={loadIfDef(phyType)}
            specAnswer={status.answer}
            submitting={rtlSubmitting}
            onSubmit={handleRtlSubmit}
            onOpenSpecDocs={() => setSpecOpen(true)}
          />
        </span>
      </div>

      <SpecDocsPanel
        phyType={phyType}
        docs={docs}
        status={status}
        open={specOpen}
        onClose={() => setSpecOpen(false)}
        onChanged={refreshSpecDocs}
      />

      <ArchitectureDiagram
        phyType={phyType}
        version={version}
        store={digitalArchStore}
        variant="digital"
        selectedBlock={selectedBlock}
        onSelectBlock={(b) => setSelectedBlock((cur) => (cur === b.id ? null : b.id))}
        topologyGroups={topologyGroups}
      />

      {selectedDesignable && (
        <BlockRtlMenu
          key={`${phyType}:${selectedDesignable.id}`}
          phyType={phyType}
          block={selectedDesignable}
          option={catalogByValue[selectedDesignable.topology]}
          libraries={libraries}
          specDocs={controllerDocs}
          submitting={rtlSubmitting}
          onSubmit={handleRtlSubmit}
        />
      )}
    </div>
  );
}
