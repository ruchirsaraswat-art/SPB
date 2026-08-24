// -----------------------------------------------------------------------------
// rx_eq_rnm.sv - RX Equalizer (CTLE + DFE) behavioral model, RNM level
// -----------------------------------------------------------------------------
// Author: Circuit_Builder (for ruchir.saraswat@gmail.com)
// Run: ser-des architecture model (1 block)
//
// What is modeled (CTLE topology):
//   - Sampled-data linear gain (DC_GAIN, V/V) applied to the real input voltage
//   - Discrete single dominant pole at F_POLE_HZ, implemented with the exact
//     ZOH one-pole update  y <= y + (1 - exp(-TS_s*wp))*(x - y)
//     evaluated on a free-running internal tick of TS_PS.
//     TS_PS = 5 ps satisfies TS <= 1/(20*f_pole) = 10 ps and TS <= 0.25 UI
//     (UI = 100 ps at the chain default 10 Gb/s).
//   - Hard output saturation at +/-VOUT_SAT (amplifier swing limit); the filter
//     state itself is clamped so the model recovers from overdrive without
//     unphysical internal wind-up.
//
// What is NOT modeled (documented judgment calls):
//   - DFE taps: the DFE half of this block needs a recovered bit clock and
//     sliced decisions (sampler/CDR blocks, not included in this run), so the
//     equalizer is modeled as its linear CTLE path only.
//   - CTLE peaking zero: no per-block spec was entered; a single dominant-pole
//     low-pass with flat DC gain is used so DC gain and the -3 dB point are
//     well-defined and verifiable. Add a zero if peaking specs arrive.
//   - No input impedance / reflections: directional RNM flow only. The input
//     is a value, not a load.
//   - Supply is a plain parameter (VDD_V), not an analog net.
//
// Parameter defaults (engineering judgment for a generic 10 Gb/s ser-des PHY):
//   DC_GAIN   = 2.0  V/V (6 dB)  - typical mid-range CTLE/VGA setting
//   F_POLE_HZ = 5e9  Hz          - dominant pole at Nyquist for 10 Gb/s NRZ
//   VOUT_SAT  = 0.6  V           - output swing limit, sane for 1.8 V class analog
//   TS_PS     = 5.0  ps          - internal model sample tick (see above)
//   VDD_V     = 1.8  V           - documentation only in v1
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module rx_eq_rnm #(
  parameter real DC_GAIN   = 2.0,    // V/V linear gain
  parameter real F_POLE_HZ = 5.0e9,  // dominant pole, Hz
  parameter real VOUT_SAT  = 0.6,    // output saturation, V (symmetric)
  parameter real TS_PS     = 5.0,    // internal model tick, ps
  parameter real VDD_V     = 1.8     // supply (informational in v1)
) (
  input  real vin,   // equalizer input voltage (real-valued RNM signal)
  output real vout   // equalized output voltage
);

  localparam real PI = 3.141592653589793;

  // Unit conversions done ONCE into local reals (never ps in the exponent).
  real ts_s;    // tick in SECONDS
  real wp;      // pole in rad/s
  real alpha;   // one-pole update coefficient
  real y;       // filter state (already includes DC gain)

  initial begin
    ts_s  = TS_PS * 1.0e-12;
    wp    = 2.0 * PI * F_POLE_HZ;
    alpha = 1.0 - $exp(-ts_s * wp);
    y     = 0.0;
    vout  = 0.0;
  end

  // Free-running model sample clock: continuous dynamics need a tick.
  always #(TS_PS) begin
    // Discrete one-pole toward the linearly amplified input
    y = y + alpha * (DC_GAIN * vin - y);
    // Saturating amplifier: clamp state and output
    if (y >  VOUT_SAT) y =  VOUT_SAT;
    if (y < -VOUT_SAT) y = -VOUT_SAT;
    vout = y;
  end

endmodule
