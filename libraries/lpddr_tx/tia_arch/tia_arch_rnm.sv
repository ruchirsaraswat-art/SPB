`timescale 1ps/1fs
// =============================================================================
// tia_arch - top-level architecture model for the optical PHY run
// -----------------------------------------------------------------------------
// Blocks included in this run: tia (tia_rnm) only. The diagram's other blocks
// (pd, optical-eq, limiting-amp, optical-slicer, optical-pll, optical-cdr,
// tx-pad, mod-driver, optical-ser) are pads / not selected, so there are no
// inter-block edges: the single block is instantiated and its natural ports
// are exposed as the chain's primary input/output.
//
// Inter-block signal convention (documented per wiring rules, applies to any
// future edges added to this top):
//   * One `real` data signal per diagram edge, directional (driver -> load):
//       - currents in amperes [A]  (photodiode-domain edges)
//       - voltages in volts   [V]  (voltage-domain edges)
//   * Clock-generating blocks would drive a bit/event clock signal to the
//     blocks they feed (none present in this run).
//   * No bidirectional termination / impedance modeling at this level.
//
// Top-level ports:
//   i_in_a  [real, A] : chain primary input  - photodiode current into the TIA.
//   v_out_v [real, V] : chain primary output - TIA output voltage.
//
// Parameters are forwarded to the block so the TB / integrator can retarget
// the model without editing block files.
// =============================================================================
module tia_arch #(
  parameter real ZT_OHM = 5000.0,
  parameter real BW_HZ  = 7.0e9,
  parameter real VSAT_V = 0.3,
  parameter real TS_PS  = 5.0,
  parameter real VDD_V  = 1.8
)(
  input  real i_in_a,   // primary input: photodiode current [A]
  output real v_out_v   // primary output: TIA voltage [V]
);

  tia_rnm #(
    .ZT_OHM (ZT_OHM),
    .BW_HZ  (BW_HZ),
    .VSAT_V (VSAT_V),
    .TS_PS  (TS_PS),
    .VDD_V  (VDD_V)
  ) u_tia (
    .i_in_a  (i_in_a),
    .v_out_v (v_out_v)
  );

endmodule
