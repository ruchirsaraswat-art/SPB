// -----------------------------------------------------------------------------
// rx_eq_rnm.sv - RNM behavioral model of an RX Equalizer (CTLE + DFE), ctle topology
//
// Model: sampled-data gain + discrete one-pole low-pass filter + hard saturation.
//   out(t) tracks A0*in(t) through a single pole at F_POLE_HZ, clamped to +/-VSAT_V.
//   Update runs on a free-running internal sample tick (TS_PS) with the exact
//   matched-pole discrete update: y <= y + (1 - exp(-TS_s*wp))*(A0*x - y),
//   where TS_s is the tick in SECONDS and wp = 2*pi*F_POLE_HZ in rad/s.
//
// Parameter defaults (engineering judgment for a generic 10 Gb/s ser-des PHY;
// no per-block specs were entered for this run):
//   A0_DC_GAIN = 4.0    -> 12 dB DC gain (typical CTLE/VGA composite gain)
//   F_POLE_HZ  = 5.0e9  -> dominant pole at Nyquist for 10 Gb/s (UI = 100 ps)
//   VSAT_V     = 0.9    -> output swing limit, half of the 1.8 V supply
//   TS_PS      = 5.0    -> internal tick; 1/(40*F_POLE) and 0.05 UI, satisfying
//                          TS <= min(1/(20*f_pole), 0.25 UI)
//   VDD_V      = 1.8    -> supply, informational only (not an analog net)
//
// Judgment calls / scope notes:
//   - Topology is "ctle": no gain-code port (that is the VGA variant) and the
//     input is a voltage (TIA variant would take a current). Ports are one
//     real voltage in, one real voltage out.
//   - CTLE peaking zero and DFE tap feedback are not exercised by this run's
//     checks; the model captures the required gain/pole/saturation behavior.
//     The filter state itself is clamped, so overdrive recovery is bounded.
//   - Directional signal flow only: the output reports a value, it presents no
//     impedance. No bidirectional termination / reflection modeling (out of
//     scope at RNM level).
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module rx_eq_rnm #(
    parameter real A0_DC_GAIN = 4.0,   // linear DC gain (12 dB)
    parameter real F_POLE_HZ  = 5.0e9, // dominant pole frequency, Hz
    parameter real VSAT_V     = 0.9,   // output saturation limit, V (symmetric)
    parameter real TS_PS      = 5.0,   // internal model sample tick, ps
    parameter real VDD_V      = 1.8    // supply voltage, informational
) (
    input  real in_v,   // equalizer input voltage
    output real out_v   // equalized output voltage
);

    // Unit conversions done ONCE into local reals (never ps in the exponent).
    real ts_s;    // tick in seconds
    real wp;      // pole in rad/s
    real alpha;   // one-pole update coefficient
    real y;       // filter state (also the clamped output)

    initial begin
        ts_s  = TS_PS * 1.0e-12;
        wp    = 2.0 * 3.14159265358979 * F_POLE_HZ;
        alpha = 1.0 - $exp(-ts_s * wp);
        y     = 0.0;
        out_v = 0.0;
    end

    // Free-running model sample clock: discrete one-pole update + saturation.
    always #(TS_PS) begin
        y = y + alpha * (A0_DC_GAIN * in_v - y);
        if (y >  VSAT_V) y =  VSAT_V;
        if (y < -VSAT_V) y = -VSAT_V;
        out_v = y;
    end

endmodule
