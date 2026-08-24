`timescale 1ps/1fs
// =============================================================================
// tia_rnm - Transimpedance Amplifier (TIA), RNM behavioral model
// -----------------------------------------------------------------------------
// Level     : RNM (real-valued ports), event/sampled-data model. No SPICE.
// Author    : Circuit_Builder (run: optical architecture model, 1 block)
//
// What is modeled:
//   * Transimpedance gain Zt [ohm]: v = Zt * i_in
//   * Single dominant pole at BW_HZ, implemented as a discrete one-pole
//     update on a free-running internal sample tick TS_PS:
//         y <= y + (1 - exp(-TS_s * wp)) * (x - y)
//     with TS_s = TS_PS * 1e-12 (tick in SECONDS) and wp = 2*pi*BW_HZ.
//     Unit conversion is done ONCE at time 0 into local reals.
//   * Hard output saturation at +/- VSAT_V (applied after the filter).
//
// What is NOT modeled (out of scope at RNM level):
//   * Bidirectional loading / input impedance - signal flow is directional
//     only; the output reports a value, it does not present an impedance.
//   * Noise, offset, sub-sample waveform shape, supply as an analog net
//     (VDD_V is a plain parameter, unused electrically in v1).
//
// Parameter defaults (engineering judgment, generic 10 Gb/s NRZ optical PHY):
//   ZT_OHM = 5000.0  : 5 kohm transimpedance - typical mid-gain TIA setting.
//   BW_HZ  = 7.0e9   : ~0.7 x 10 Gb/s data rate, classic RX front-end BW.
//   VSAT_V = 0.3     : +/-300 mV output swing limit (differential-equivalent).
//                      Full-swing input current = VSAT_V/ZT_OHM = 60 uA.
//   TS_PS  = 5.0     : sample tick; satisfies TS <= 1/(20*BW) = 7.14 ps and
//                      TS <= 0.25 UI (25 ps at 10 Gb/s).
//   VDD_V  = 1.8     : supply annotation only.
// =============================================================================
module tia_rnm #(
  parameter real ZT_OHM = 5000.0,  // transimpedance gain [ohm]
  parameter real BW_HZ  = 7.0e9,   // -3 dB bandwidth (single pole) [Hz]
  parameter real VSAT_V = 0.3,     // output saturation limit [V], symmetric
  parameter real TS_PS  = 5.0,     // internal model sample tick [ps]
  parameter real VDD_V  = 1.8      // supply annotation [V] (not modeled as net)
)(
  input  real i_in_a,   // photodiode current in [A]
  output real v_out_v   // TIA output voltage [V]
);

  localparam real PI = 3.141592653589793;

  real y;      // one-pole filter state [V]
  real alpha;  // discrete-pole coefficient, computed once at t=0

  initial begin
    // Unit conversions done ONCE: ps -> s, Hz -> rad/s.
    real ts_s, wp;
    ts_s    = TS_PS * 1.0e-12;
    wp      = 2.0 * PI * BW_HZ;
    alpha   = 1.0 - $exp(-ts_s * wp);
    y       = 0.0;
    v_out_v = 0.0;
  end

  // Free-running model sample tick.
  always #(TS_PS) begin
    y <= y + alpha * (ZT_OHM * i_in_a - y);
  end

  // Saturation applied to filtered value.
  always @(y) begin
    if      (y >  VSAT_V) v_out_v =  VSAT_V;
    else if (y < -VSAT_V) v_out_v = -VSAT_V;
    else                  v_out_v =  y;
  end

endmodule
