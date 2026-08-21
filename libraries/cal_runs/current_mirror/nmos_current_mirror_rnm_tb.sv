// ---------------------------------------------------------------------------
// nmos_current_mirror_rnm_tb.sv
// Self-checking TB for nmos_current_mirror_rnm (RNM, real-valued ports).
// Date: 2026-08-20.  Run: cycle3 calibration 10uA 1:2 mirror.
// Checks (target = 22.1166 uA = SPICE-achieved Iout, vcompliance = 0.9 V):
//   1. DC sweep vout 0..1.8 V: iout within 5 percent of target for all
//      vout at or above vcompliance
//   2. iout below 80 percent of target at vout = vcompliance/3 = 0.3 V
//      (compliance rolloff exists)
//   3. ratio iout/iref at vout = 0.9 V within 1 percent of 2.21166
// Settling tau: not applicable - static block, no dynamics in this run
// (documented per RNM rules; nothing to derive from a load cap here).
// Prints exactly one of RNM_TB_PASS or RNM_TB_FAIL plus a reason.
// Global watchdog: 2,000,000 ps (about 20x the expected 90,000 ps run).
// Verification status: generated_unverified - iverilog not installed here.
// ---------------------------------------------------------------------------

`timescale 1ps/1fs

module nmos_current_mirror_rnm_tb;

  localparam real TARGET_UA = 22.1166;
  localparam real VCOMP_V   = 0.9;
  localparam real RATIO_EXP = 2.21166;
  localparam integer WATCHDOG_PS = 2000000;

  real iref;
  real vout;
  real iout;
  real err;
  real worst_err;
  real ratio_meas;
  integer i;
  integer fails;

  nmos_current_mirror_rnm dut (
    .iref_ua (iref),
    .vout_v  (vout),
    .iout_ua (iout)
  );

  // global watchdog - a silent hang is never acceptable
  initial begin #WATCHDOG_PS; $display("RNM_TB_FAIL watchdog timeout"); $finish; end

  initial begin
    fails = 0;
    worst_err = 0.0;
    iref = 10.0;
    vout = 0.0; #100;

    // check 1: flatness at and above compliance (10 mV steps, 0 to 1.8 V)
    for (i = 0; i <= 180; i = i + 1) begin
      vout = 0.01 * i; #100;
      if (vout >= VCOMP_V - 1.0e-9) begin
        err = (iout - TARGET_UA) / TARGET_UA;
        if (err < 0.0) err = -err;
        if (err > worst_err) worst_err = err;
        if (err > 0.05) fails = fails + 1;
      end
    end
    $display("RNM_INFO worst flatness error above compliance = %g", worst_err);

    // check 2: rolloff at vcompliance/3
    vout = VCOMP_V / 3.0; #100;
    $display("RNM_INFO iout at vc/3 = %g uA (target %g uA)", iout, TARGET_UA);
    if (iout >= 0.8 * TARGET_UA) begin
      fails = fails + 1;
      $display("RNM_INFO rolloff check failed - always-on source?");
    end

    // check 3: mirror ratio at vout = vcompliance
    vout = VCOMP_V; #100;
    ratio_meas = iout / iref;
    $display("RNM_INFO ratio at vout=0.9V = %g (expected %g)", ratio_meas, RATIO_EXP);
    err = (ratio_meas - RATIO_EXP) / RATIO_EXP;
    if (err < 0.0) err = -err;
    if (err > 0.01) fails = fails + 1;

    if (fails == 0) $display("RNM_TB_PASS");
    else $display("RNM_TB_FAIL %0d check(s) failed", fails);
    $finish;
  end

endmodule
