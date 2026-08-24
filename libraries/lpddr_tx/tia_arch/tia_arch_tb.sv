`timescale 1ps/1fs
// =============================================================================
// tia_arch_tb - self-checking testbench for the tia_arch architecture model
// -----------------------------------------------------------------------------
// Checks (per run spec):
//   1) DC transimpedance: small-signal DC current in -> v_out/i_in within 10%
//      of ZT_OHM (and within 1 dB).
//   2) Pole-frequency response: sine current at BW_HZ for >= 20 cycles,
//      steady-state output/input amplitude ratio must be -3 dB +/- 1 dB
//      relative to Zt (catches sample-tick aliasing). Amplitude measured by
//      min/max peak detection - no FFT.
//   3) Overdrive/saturation: DC input at 3x the full-swing current
//      (3 * VSAT/Zt), both polarities: |v_out| <= VSAT*1.05, and the output
//      must actually reach >= 0.9*VSAT (proves it responds and clips).
//   4) No X/NaN, output responds to input; global watchdog timeout.
// Prints exactly one of: ARCH_TB_PASS  or  ARCH_TB_FAIL <reason>.
// =============================================================================
module tia_arch_tb;

  // Must match DUT defaults (also forwarded explicitly).
  localparam real ZT_OHM = 5000.0;
  localparam real BW_HZ  = 7.0e9;
  localparam real VSAT_V = 0.3;
  localparam real TS_PS  = 5.0;
  localparam real PI     = 3.141592653589793;

  localparam real WATCHDOG_PS = 400000.0;  // ~20x expected ~15 ns test time

  real i_in_a;
  wire real v_out_v;

  tia_arch #(
    .ZT_OHM (ZT_OHM),
    .BW_HZ  (BW_HZ),
    .VSAT_V (VSAT_V),
    .TS_PS  (TS_PS)
  ) dut (
    .i_in_a  (i_in_a),
    .v_out_v (v_out_v)
  );

  // ---------------- global watchdog (mandatory) ----------------
  initial begin
    #(WATCHDOG_PS);
    $display("ARCH_TB_FAIL watchdog timeout");
    $finish;
  end

  // ---------------- sine generator for the AC test ----------------
  reg  ac_en;
  real ac_amp_a;
  initial begin
    ac_en    = 1'b0;
    ac_amp_a = 0.0;
  end
  always begin
    #1.0; // 1 ps update step: ~142 points per 7 GHz cycle
    if (ac_en) i_in_a = ac_amp_a * $sin(2.0*PI*BW_HZ*($realtime*1.0e-12));
  end

  // ---------------- peak detector ----------------
  reg  meas_en;
  real vmax, vmin;
  initial begin
    meas_en = 1'b0;
    vmax = -1.0e9; vmin = 1.0e9;
  end
  always begin
    #1.0;
    if (meas_en) begin
      if (v_out_v > vmax) vmax = v_out_v;
      if (v_out_v < vmin) vmin = v_out_v;
    end
  end

  // ---------------- main sequence ----------------
  real vdc, zt_meas, gain_db_err;
  real amp_meas, amp_ratio_db;
  initial begin
    i_in_a = 0.0;
    #200.0; // let model tick a few times at zero input

    if (v_out_v != v_out_v) begin // NaN check
      $display("ARCH_TB_FAIL output is NaN at start"); $finish;
    end
    if (v_out_v > 1.0e-3 || v_out_v < -1.0e-3) begin
      $display("ARCH_TB_FAIL nonzero output with zero input (%g V)", v_out_v);
      $finish;
    end

    // ---- Test 1: DC transimpedance (20 uA -> expect 100 mV) ----
    i_in_a = 20.0e-6;
    #3000.0; // >> 130 time constants (tau ~ 22.7 ps)
    vdc = v_out_v;
    if (vdc < 1.0e-6) begin
      $display("ARCH_TB_FAIL output stuck / does not respond to DC input (%g V)", vdc);
      $finish;
    end
    zt_meas = vdc / 20.0e-6;
    gain_db_err = 20.0*$log10(zt_meas/ZT_OHM);
    $display("INFO DC: v_out=%g V, Zt_meas=%g ohm (target %g), err=%g dB",
             vdc, zt_meas, ZT_OHM, gain_db_err);
    if (zt_meas > ZT_OHM*1.10 || zt_meas < ZT_OHM*0.90) begin
      $display("ARCH_TB_FAIL DC transimpedance %g ohm outside 10%% of %g ohm",
               zt_meas, ZT_OHM);
      $finish;
    end
    if (gain_db_err > 1.0 || gain_db_err < -1.0) begin
      $display("ARCH_TB_FAIL DC gain error %g dB exceeds 1 dB", gain_db_err);
      $finish;
    end

    // ---- Test 2: response at the pole frequency (aliasing check) ----
    i_in_a   = 0.0;
    #1000.0;
    ac_amp_a = 10.0e-6;             // 50 mV*Zt peak << VSAT: stays linear
    ac_en    = 1'b1;
    #3000.0;                        // settle ~21 cycles
    vmax = -1.0e9; vmin = 1.0e9;
    meas_en = 1'b1;
    #3000.0;                        // measure over ~21 cycles (>= 20)
    meas_en = 1'b0;
    ac_en   = 1'b0;
    i_in_a  = 0.0;
    amp_meas     = (vmax - vmin) / 2.0;
    amp_ratio_db = 20.0*$log10(amp_meas / (ac_amp_a * ZT_OHM));
    $display("INFO AC@pole: amp=%g V (ideal -3dB: %g V), rel gain = %g dB",
             amp_meas, ac_amp_a*ZT_OHM/1.4142135624, amp_ratio_db);
    if (amp_ratio_db > -2.0 || amp_ratio_db < -4.0) begin
      $display("ARCH_TB_FAIL pole-frequency response %g dB not within -3 +/- 1 dB",
               amp_ratio_db);
      $finish;
    end

    // ---- Test 3: overdrive / saturation, 3x full-swing input ----
    i_in_a = 3.0 * (VSAT_V / ZT_OHM);   // +180 uA
    #3000.0;
    $display("INFO OVR+: v_out=%g V (limit %g V)", v_out_v, VSAT_V);
    if (v_out_v > VSAT_V*1.05 || v_out_v < -VSAT_V*1.05) begin
      $display("ARCH_TB_FAIL positive overdrive output %g V exceeds sat %g V +5%%",
               v_out_v, VSAT_V);
      $finish;
    end
    if (v_out_v < 0.9*VSAT_V) begin
      $display("ARCH_TB_FAIL positive overdrive output %g V never reached 0.9*VSAT",
               v_out_v);
      $finish;
    end

    i_in_a = -3.0 * (VSAT_V / ZT_OHM);  // -180 uA
    #3000.0;
    $display("INFO OVR-: v_out=%g V (limit -%g V)", v_out_v, VSAT_V);
    if (v_out_v < -VSAT_V*1.05 || v_out_v > VSAT_V*1.05) begin
      $display("ARCH_TB_FAIL negative overdrive output %g V exceeds sat limit",
               v_out_v);
      $finish;
    end
    if (v_out_v > -0.9*VSAT_V) begin
      $display("ARCH_TB_FAIL negative overdrive output %g V never reached -0.9*VSAT",
               v_out_v);
      $finish;
    end

    $display("ARCH_TB_PASS");
    $finish;
  end

endmodule
