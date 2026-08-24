// The "PHY Type / Architecture" pull-down, extracted from SpecForm (user
// request, 2026-08-23): it now lives in the top-level "PHY architecture"
// overview panel's header bar, since picking a PHY type re-targets ALL
// three workspaces (AFE diagram, interface editor, controller diagram),
// not just the spec form. Kept in the header (not the body) so it stays
// reachable even when the overview panel is minimized.
//
// Value contract is unchanged: a built-in phy_type value, or
// "custom:<name>" for a saved architecture from GET /api/architectures
// (the "Custom (saved architectures)" optgroup). App owns the choice and
// pushes the resulting phy_type down into SpecForm.
export default function PhyTypeSelect({ archChoice, customArchs = [], onChange }) {
  return (
    <label className="phy-type-select" onClick={(e) => e.stopPropagation()}>
      <span>PHY Type / Architecture</span>
      <select value={archChoice} onChange={(e) => onChange(e.target.value)}>
        <optgroup label="Built-in PHY types">
          <option value="ser-des">SerDes (Serializer/Deserializer)</option>
          <option value="ddr">DDR (Double Data Rate Memory)</option>
          <option value="lpddr">LPDDR (Low-Power DDR Memory)</option>
          <option value="hbm">HBM (High-Bandwidth Memory)</option>
          <option value="optical">Optical (Optical Communication)</option>
          <option value="die-to-die">Die-to-Die (Chiplet Interconnect)</option>
        </optgroup>
        {customArchs.length > 0 && (
          <optgroup label="Custom (saved architectures)">
            {customArchs.map((a) => (
              <option key={a.name} value={`custom:${a.name}`}>
                {a.label || a.name}
                {a.phy_type ? ` — ${a.phy_type}` : ""}
              </option>
            ))}
          </optgroup>
        )}
      </select>
    </label>
  );
}
