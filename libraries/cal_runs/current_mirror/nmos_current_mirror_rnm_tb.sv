// -----------------------------------------------------------------------------
// Block:        nmos_current_mirror_rnm_tb
// Description:  Self-checking testbench for nmos_current_mirror_rnm (RNM)
// Author:       Circuit_Builder (for ruchir.saraswat@gmail.com)
// Date:         2026-08-21
// Abstraction:  RNM testbench (real-valued stimulus, plain real ports)
//
// Parameter <-> spec-field mapping (checks performed)
// ---------------------------------------------------------------------------
//   Check                          Spec field         Target        Tolerance
//   nominal iout @ vout=VGS_REF    iout (20.0 uA)     20.0e-6 A     +/-0.5%
//   mirror ratio iout/iref         ratio 2.0:1.0      2.0           +/-0.5%
//   output resistance              judgment 500 kOhm  500e3 Ohm     +/-5%
//   ratio tempco (27C -> 127C)     judgment 100ppm/C  100 ppm/C     within 2x
//   compliance rolloff @ 0.1 V     judgment Vdsat=0.2 iout < 60% of sat value
//   1%-settling at 5*tau           derived tau=500ps  |err| < 1% of step
//   ~63% point at 1*tau            derived tau=500ps  remaining frac 0.2..0.5
//     (the 1*tau check catches ps/seconds exponent mixups: an all-pass model
//      settles instantly, a frozen model never moves - both fail this window)
//
// NOT modeled/checked: sub-1% absolute accuracy, corners, noise, mismatch.
// Verification status: VERIFIED (prints RNM_TB_PASS; see rnm_sim.log)
// Watchdog: 1,000,000 ps ~ 20x expected ~50 ns test duration.
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module nmos_current_mirror_rnm_tb;

  // spec / judgment constants (mirror the DUT defaults)
  localparam real IREF_A    = 10.0e-6;
  localparam real RATIO     = 2.0;
  localparam real ROUT_OHM  = 500.0e3;
  localparam real VGS_REF_V = 0.65;
  localparam real TC_PPM_C  = 100.0;
  localparam real TAU_PS    = 500.0;   // derived in DUT header: Cg/gm = 500 ps
  localparam real SETTLE_PS = 5000.0;  // 10*tau: "fully settled" wait

  real iref, vout, temp;
  real iout;
  integer nfail;

  // measurement temporaries
  real i_nom, i_hi, rout_meas, i_hot, tc_meas, i_compl;
  real i0, i1, i5, ifin, frac1, frac5;

  nmos_current_mirror_rnm dut (
    .iref_a (iref),
    .vout_v (vout),
    .temp_c (temp),
    .iout_a (iout)
  );

  // global watchdog: a hang must fail loudly, never stall vvp
  initial begin
    #1000000;
    $display("RNM_TB_FAIL watchdog timeout");
    $finish;
  end

  task check(input string name, input real meas, input real lo, input real hi);
    begin
      if (meas >= lo && meas <= hi)
        $display("  ok   %s = %g  (window %g .. %g)", name, meas, lo, hi);
      else begin
        $display("  FAIL %s = %g  outside window %g .. %g", name, meas, lo, hi);
        nfail = nfail + 1;
      end
    end
  endtask

  initial begin
    nfail = 0;
    iref  = IREF_A;
    temp  = 27.0;
    vout  = VGS_REF_V;   // vds(out) = vds(ref): ratio point, no rout term

    // --- 1) nominal output current and mirror ratio ---
    #(SETTLE_PS);
    i_nom = iout;
    check("iout_nominal_A", i_nom, 20.0e-6*0.995, 20.0e-6*1.005);
    check("mirror_ratio",   i_nom/iref, RATIO*0.995, RATIO*1.005);
    $display("MEAS iout_ua=%0.6f", i_nom*1.0e6);
    $display("MEAS ratio=%0.6f",   i_nom/iref);

    // --- 2) output resistance: delta-V / delta-I between 0.65 V and 1.65 V ---
    vout = VGS_REF_V + 1.0;
    #(SETTLE_PS);
    i_hi = iout;
    rout_meas = 1.0 / (i_hi - i_nom);
    check("rout_ohm", rout_meas, ROUT_OHM*0.95, ROUT_OHM*1.05);
    $display("MEAS rout_kohm=%0.3f", rout_meas/1.0e3);

    // --- 3) tempco: 27C -> 127C at the ratio point, within 2x of judgment ---
    vout = VGS_REF_V;
    temp = 127.0;
    #(SETTLE_PS);
    i_hot   = iout;
    tc_meas = ((i_hot/i_nom) - 1.0) / (127.0 - 27.0) * 1.0e6;  // ppm/C
    check("tempco_ppm_c", tc_meas, TC_PPM_C/2.0, TC_PPM_C*2.0);
    $display("MEAS tempco_ppm_c=%0.3f", tc_meas);
    temp = 27.0;

    // --- 4) compliance: vout = 0.1 V (< Vdsat = 0.2 V) rolls the current off ---
    vout = 0.1;
    #(SETTLE_PS);
    i_compl = iout;
    check("compliance_iout_A", i_compl, 1.0e-9, 0.60*i_nom);
    $display("MEAS iout_compliance_ua=%0.6f", i_compl*1.0e6);

    // --- 5) settling: step vout 0.65 -> 1.65, one-pole with tau = 500 ps ---
    vout = VGS_REF_V;
    #(SETTLE_PS);
    i0   = iout;                 // pre-step settled value
    vout = VGS_REF_V + 1.0;      // step
    #(TAU_PS);
    i1 = iout;                   // at 1*tau: expect ~63% settled
    #(4.0*TAU_PS);
    i5 = iout;                   // at 5*tau: expect within 1%
    #(5.0*TAU_PS);
    ifin = iout;                 // fully settled reference
    frac1 = (ifin - i1) / (ifin - i0);   // remaining fraction at 1*tau (~0.368)
    frac5 = (ifin - i5) / (ifin - i0);   // remaining fraction at 5*tau (<0.01)
    check("settle_remaining_at_1tau", frac1, 0.20, 0.50);
    check("settle_remaining_at_5tau_pct", frac5*100.0, -1.0, 1.0);
    $display("MEAS settle_frac_1tau=%0.4f settle_frac_5tau_pct=%0.4f",
             frac1, frac5*100.0);

    // --- verdict: exactly one machine-greppable token ---
    if (nfail == 0)
      $display("RNM_TB_PASS");
    else
      $display("RNM_TB_FAIL %0d check(s) failed", nfail);
    $finish;
  end

endmodule
