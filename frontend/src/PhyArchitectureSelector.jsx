import { PHY_DESCRIPTIONS, blocksForPhy } from "./phyArchitectures";
import ArchitectureDiagram from "./ArchitectureDiagram";

export default function PhyArchitectureSelector({ phy_type, selectedBlock, blockStates, topologyOptions, onSelect }) {
  if (!phy_type) return null;
  const blocks = blocksForPhy(phy_type);

  return (
    <div className="phy-selector">
      <h3>PHY Architecture: {phy_type}</h3>
      <p className="hint">{PHY_DESCRIPTIONS[phy_type]}</p>

      {blocks.length === 0 ? (
        <p className="hint">No blocks available for this PHY yet</p>
      ) : (
        <ArchitectureDiagram
          phyType={phy_type}
          blockStates={blockStates}
          selectedBlock={selectedBlock}
          topologyOptions={topologyOptions}
          onSelectBlock={onSelect}
        />
      )}
    </div>
  );
}
