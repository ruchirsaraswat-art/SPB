// -----------------------------------------------------------------------------
// Block:        nmos_current_mirror_rnm
// Description:  RNM (real-number model) of a 2-transistor NMOS current mirror
// Author:       Circuit_Builder (for ruchir.saraswat@gmail.com)
// Date:         2026-08-21
// Abstraction:  RNM / real-valued-port SystemVerilog (Icarus-compatible:
//               plain `input real` / `output real` variable ports; no nettype,
//               no wreal, no Verilog-AMS)
//
// Parameter <-> spec-field mapping
// ---------------------------------------------------------------------------
//   Parameter        Spec field / origin                       Default
//   IREF_A           iref_ua = 10.0 uA                         10.0e-6 A
//   RATIO            ratio out:ref = 2.0:1.0                   2.0
//   VDD_V            vdd_v = 1.8 V                             1.8
//   TEMP_NOM_C       temp_c = 27.0 C                           27.0
//   W_REF_UM         reference device W = 2.0 um (info only)   2.0
//   L_UM             device L = 0.5 um (info only)             0.5
//   ROUT_OHM         NOT SPECIFIED - judgment: 500 kOhm.       500.0e3
//                    (lambda ~ 0.1/V at L=0.5um -> ro ~ 1/(lambda*20uA))
//   VGS_REF_V        NOT SPECIFIED - judgment: 0.65 V diode    0.65
//                    node voltage (sky130 nfet Vt~0.45 + Vov~0.2)
//   VDSAT_V          NOT SPECIFIED - judgment: 0.20 V          0.20
//                    compliance/saturation onset at output
//   RATIO_TC_PPM_C   NOT SPECIFIED - judgment: 100 ppm/C       100.0
//                    mirror-ratio temperature drift
//   CG_TOT_F         NOT SPECIFIED - judgment: 50 fF total     50.0e-15
//                    gate-node capacitance (both gates)
//   GM_REF_S         derived judgment: gm = 2*Iref/Vov         100.0e-6
//                    = 2*10uA/0.2V = 100 uA/V
//   TS_PS            model sample tick, ps                     25.0
//
// Settling-tau derivation (documented per RNM rules; no spec field defines it):
//   tau = CG_TOT_F / GM_REF_S = 50e-15 / 100e-6 = 500 ps  (=> f_pole ~ 318 MHz)
//   The TB checks 1%-settling against 5*tau = 2500 ps (exp(-5) = 0.67% < 1%).
//   Sample tick TS = 25 ps = tau/20 <= 1/(20*f_pole) -> no aliasing of the pole.
//   Unit conversion done ONCE: ts_s = TS_PS*1e-12; exponent uses seconds only.
//
// Behavior modeled:
//   iout = RATIO*iref*(1 + tc*(T-27)) + (vout - VGS_REF_V)/ROUT_OHM  (saturation)
//   linear (triode-like) rolloff for vout < VDSAT_V, clamp to 0 for vout <= 0
//   first-order (one-pole) settling of iout with the derived tau above
//
// NOT modeled:
//   - Bidirectional impedance / loading (directional RNM flow only: iout is a
//     told value, presents no impedance back to the driver)
//   - Sub-sample waveform shape, accuracy below ~1% (Verilog-A/SPICE territory)
//   - Supply as an analog net (VDD_V is a plain real parameter), noise,
//     mismatch statistics, process corners (tt values baked into judgments)
//
// Verification status: VERIFIED - see nmos_current_mirror_rnm_tb.sv and
//   rnm_sim.log (token RNM_TB_PASS, iverilog -g2012 / vvp, Icarus 12.0)
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module nmos_current_mirror_rnm #(
  parameter real IREF_A         = 10.0e-6,
  parameter real RATIO          = 2.0,
  parameter real VDD_V          = 1.8,
  parameter real TEMP_NOM_C     = 27.0,
  parameter real W_REF_UM       = 2.0,
  parameter real L_UM           = 0.5,
  parameter real ROUT_OHM       = 500.0e3,
  parameter real VGS_REF_V      = 0.65,
  parameter real VDSAT_V        = 0.20,
  parameter real RATIO_TC_PPM_C = 100.0,
  parameter real CG_TOT_F       = 50.0e-15,
  parameter real GM_REF_S       = 100.0e-6,
  parameter real TS_PS          = 25.0
)(
  input  real iref_a,   // reference branch current [A]
  input  real vout_v,   // output (mirror drain) node voltage [V]
  input  real temp_c,   // junction temperature [C]
  output real iout_a    // mirrored output current [A]
);

  // one-time unit conversions (ps -> s done ONCE, exponent sees seconds only)
  real tau_s, ts_s, alpha;

  initial begin
    tau_s  = CG_TOT_F / GM_REF_S;        // 500e-12 s with defaults
    ts_s   = TS_PS * 1e-12;              // tick in SECONDS
    alpha  = 1.0 - $exp(-ts_s / tau_s);  // discrete one-pole coefficient
    iout_a = 0.0;
  end

  // free-running model sample clock: TS = tau/20 (<= 1/(20*f_pole))
  always #(TS_PS) begin
    real itgt;
    // static (settled) target current
    itgt = RATIO * iref_a * (1.0 + RATIO_TC_PPM_C*1.0e-6*(temp_c - TEMP_NOM_C));
    itgt = itgt + (vout_v - VGS_REF_V) / ROUT_OHM;
    // output compliance: linear rolloff below VDSAT, hard zero at/below 0 V
    if (vout_v <= 0.0)
      itgt = 0.0;
    else if (vout_v < VDSAT_V)
      itgt = itgt * (vout_v / VDSAT_V);
    // discrete one-pole update: y <= y + (1 - exp(-TS_s/tau_s))*(x - y)
    iout_a <= iout_a + alpha * (itgt - iout_a);
  end

endmodule
