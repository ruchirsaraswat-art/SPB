// Top-level "PHY architecture" overview (user request, 2026-08-23): the
// whole PHY depicted as its two halves - the AFE (analog front end) and the
// Controller (the digital side) - joined by the AFE<->digital interface,
// between the line/channel and the SoC core. Purely navigational: clicking
// a block focuses the corresponding workspace panel below (and with it the
// chat sidebar context), un-minimizes it if collapsed, and scrolls it into
// view (App.jsx handleOverviewFocus - with the stacked layout the target
// can be off-screen, so focus alone looked like a no-op). No state of its
// own - the real content lives in the three workspaces.

// Focus mode (2026-08-27, "Focus: DDR AFE only"): the depiction keeps all
// four blocks (the PHY is still honestly a PHY) but the IF / Controller /
// Firmware blocks are greyed out and disabled - their workspaces are not
// rendered while focus is on, so clicking would go nowhere. The hint on
// each says how to bring them back (Settings toggle).
const FOCUS_HINT = 'Out of scope while "Focus: DDR AFE only" is on — toggle it off in Settings to work on this';

export default function PhyOverviewPanel({ phyType, focusedWorkspace, onFocus, focusDdrAfe = false }) {
  return (
    <div className="phy-overview">
      <p className="hint">
        The {phyType} PHY at the top level: the analog front end and the
        digital controller, joined by the AFE ↔ controller interface, operated
        by firmware on the SoC. Click a block to jump to its workspace.
      </p>
      <div className="phy-overview-row">
        <div className="phy-overview-ext">
          Line /<br />channel
        </div>
        <span className="phy-overview-wire">⇄</span>
        <button
          type="button"
          className={`phy-overview-block ${focusedWorkspace === "afe" ? "active" : ""}`}
          onClick={() => onFocus("afe")}
          title="Focus the PHY / AFE architecture workspace"
        >
          <strong>AFE</strong>
          <span>Analog front end — termination, EQ, clocking, ser/des</span>
        </button>
        <button
          type="button"
          className={`phy-overview-iface ${focusedWorkspace === "interface" ? "active" : ""} ${focusDdrAfe ? "focus-dimmed" : ""}`}
          onClick={() => onFocus("interface")}
          disabled={focusDdrAfe}
          title={focusDdrAfe ? FOCUS_HINT : "Focus the AFE ↔ Controller interface workspace"}
        >
          <strong>IF</strong>
          <span>signals · clocks · CDC · sequences</span>
        </button>
        <button
          type="button"
          className={`phy-overview-block ${focusedWorkspace === "digital" ? "active" : ""} ${focusDdrAfe ? "focus-dimmed" : ""}`}
          onClick={() => onFocus("digital")}
          disabled={focusDdrAfe}
          title={focusDdrAfe ? FOCUS_HINT : "Focus the PHY / Controller architecture workspace"}
        >
          <strong>Controller</strong>
          <span>Digital datapath & control — align/framing, training, CSR</span>
        </button>
        <span className="phy-overview-wire">⇄</span>
        {/* Firmware sits in the CONTROL-PLANE chain between the controller
            and the SoC (2026-08-23 firmware-section spec, section c): it is
            the bus master on the CSR/APB side and the only agent the SoC
            application uses to operate the PHY. Styled as SOFTWARE (distinct
            fill + dashed border + "SW" tag) - it is not a hardware pipeline
            stage the data flows through; the caption below keeps the data
            path honest. */}
        <button
          type="button"
          className={`phy-overview-block phy-overview-firmware ${focusedWorkspace === "firmware" ? "active" : ""} ${focusDdrAfe ? "focus-dimmed" : ""}`}
          onClick={() => onFocus("firmware")}
          disabled={focusDdrAfe}
          title={focusDdrAfe ? FOCUS_HINT : "Focus the PHY / Firmware workspace"}
        >
          <strong>
            Firmware <span className="phy-overview-sw-tag">SW</span>
          </strong>
          <span>Bare-metal C on the SoC — init/power sequencing, training & cal supervisors, register driver</span>
        </button>
        <span className="phy-overview-wire">⇄</span>
        <div className="phy-overview-ext">
          SoC core /<br />host
        </div>
      </div>
      <p className="phy-overview-caption">
        Control plane: SoC operates the PHY through firmware. Data path is
        Controller ⇄ SoC direct (core interface).
      </p>
      {focusDdrAfe && (
        <p className="phy-overview-caption focus-caption">
          Focus: DDR AFE only — interface, controller and firmware workspaces
          are hidden (coming later). Toggle off in Settings to restore them.
        </p>
      )}
    </div>
  );
}
