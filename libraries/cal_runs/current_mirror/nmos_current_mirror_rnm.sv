// ---------------------------------------------------------------------------
// nmos_current_mirror_rnm.sv
// Block            : NMOS current mirror, 1:2 (sky130 sky130_fd_pr__nfet_01v8)
// Date             : 2026-08-20
// Abstraction      : RNM real-number model (SystemVerilog, variable-type
//                    real ports - not nettype real, not wreal)
// Run label        : cycle3 calibration: 10uA 1:2 mirror + veriloga/rnm models
//
// Parameter to spec-field mapping (defaults = what the transistor-level
// design ACTUALLY ACHIEVED in the ngspice .op at tt / 27C / VDD=1.8V):
//   iref_ua_nom    maps to iref_ua       : target 10.0 uA, achieved 10.0
//   ratio          maps to ratio_out/ratio_ref : target 2.0, SPICE-achieved
//                  2.21166 (22.1166 uA / 10.0 uA), so default = 2.21166
//   vcompliance_v  maps to vds_mirror_v  : achieved Vds headroom = 0.9 V
//   vdd_v          maps to vdd_v         : 1.8 V (plain real parameter)
//
// Dynamics / settling tau: this mirror is a STATIC block in this run (no
// load cap or settling spec field), so the model is purely combinational
// and no sample clock or settling tau applies - documented here per the
// RNM rules. Continuous dynamics would need an always #TS tick.
//
// NOT modeled: device noise, mismatch, temperature dependence, sub-1-percent
// absolute accuracy (SPICE/Verilog-A territory), bidirectional loading or
// output impedance (directional real-value flow only), supply as an analog
// net (vdd_v is a plain real parameter).
//
// Verification status: generated_unverified - iverilog is not installed on
// this machine, so the self-checking TB (nmos_current_mirror_rnm_tb.sv)
// could not be compiled or run. See summary.json.
// ---------------------------------------------------------------------------

`timescale 1ps/1fs

module nmos_current_mirror_rnm #(
  parameter real iref_ua_nom   = 10.0,
  parameter real ratio         = 2.21166,
  parameter real vcompliance_v = 0.9,
  parameter real vdd_v         = 1.8
)(
  input  real iref_ua,   // reference branch current, uA
  input  real vout_v,    // voltage at the mirror output node, V
  output real iout_ua    // mirrored output current, uA
);

  // smooth tanh(3*v/vc) compliance rolloff, same shape as the Verilog-A model
  function real mirror_iout(input real iref, input real v);
    real x;
    real e2x;
    real th;
    begin
      x = 3.0 * v / vcompliance_v;
      if (x > 20.0) th = 1.0;
      else if (x < -20.0) th = -1.0;
      else begin
        e2x = $exp(2.0 * x);
        th  = (e2x - 1.0) / (e2x + 1.0);
      end
      mirror_iout = iref * ratio * th;
    end
  endfunction

  // static model: recompute on any input change (no sample clock needed)
  always @(iref_ua, vout_v) iout_ua = mirror_iout(iref_ua, vout_v);

  initial iout_ua = mirror_iout(iref_ua, vout_v);

endmodule
