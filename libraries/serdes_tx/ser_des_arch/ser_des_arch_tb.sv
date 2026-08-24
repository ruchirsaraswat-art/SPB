// -----------------------------------------------------------------------------
// ser_des_arch_tb.sv - self-checking testbench for ser_des_arch (RNM)
// -----------------------------------------------------------------------------
// Checks on the rx-eq (CTLE) path through the top module:
//   1. DC gain: vin = 0.1 V -> vout = DC_GAIN*0.1 within +/-1 dB
//      (also proves the output responds and is not stuck/NaN)
//   2. Pole-frequency response: 5 GHz sine (== F_POLE_HZ), 0.1 V amplitude,
//      10 cycles settle + 20 cycles measured; steady-state output/input
//      amplitude ratio must be DC_GAIN/sqrt(2) within +/-1 dB.
//      This catches sample-tick aliasing (no FFT, per RNM rules).
//   3. Overdrive/saturation: DC input = 3x the level that produces full output
//      swing (3 * VOUT_SAT/DC_GAIN = 0.9 V), both polarities; |vout| must stay
//      <= VOUT_SAT * 1.05 AND actually reach >= 0.9*VOUT_SAT (proves the clamp
//      exists rather than the signal just being small).
//   Mandatory global watchdog: 400 ns ~ 20x the ~15 ns test duration.
// Prints exactly one of: ARCH_TB_PASS / ARCH_TB_FAIL <reason>.
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module ser_des_arch_tb;

  // Must mirror the DUT defaults in rx_eq_rnm.sv
  localparam real DC_GAIN   = 2.0;
  localparam real F_POLE_HZ = 5.0e9;
  localparam real VOUT_SAT  = 0.6;
  localparam real PI        = 3.141592653589793;

  localparam real DB1_HI = 1.1220;   // +1 dB as linear ratio
  localparam real DB1_LO = 1.0/1.1220;

  real vin;
  real vout;

  ser_des_arch dut (
    .rx_in_v  (vin),
    .rx_out_v (vout)
  );

  // ---------------- mandatory global watchdog ----------------
  initial begin
    #400000; // 400 ns ~ 20x expected test duration
    $display("RNM_TB_FAIL watchdog timeout");
    $display("ARCH_TB_FAIL watchdog timeout");
    $finish;
  end

  task automatic fail(input string msg);
    begin
      $display("ARCH_TB_FAIL %s", msg);
      $finish;
    end
  endtask

  // Sine driver at the pole frequency, updated every 1 ps (200 pts/period)
  reg sine_en;
  always #1 begin
    if (sine_en) vin = 0.1 * $sin(2.0 * PI * F_POLE_HZ * 1.0e-12 * $realtime);
  end

  integer i;
  real vexp, vmax, vmin, amp, exp_amp;

  initial begin
    sine_en = 0;
    vin = 0.0;
    #500;

    // -------- Test 1: DC gain within 1 dB --------
    vin = 0.1;
    #2000; // ~63 time constants of the 5 GHz pole -> fully settled
    vexp = DC_GAIN * 0.1;
    if (vout != vout)                     fail("output is NaN");
    if (vout <= 0.0)                      fail("output stuck / does not respond to input");
    if (vout < vexp*DB1_LO || vout > vexp*DB1_HI)
      fail($sformatf("DC gain out of 1 dB tolerance: vout=%g expected=%g", vout, vexp));
    $display("TB: DC check ok, vout=%g V (expected %g V)", vout, vexp);

    // -------- Test 2: -3 dB at the pole frequency (aliasing check) --------
    vin = 0.0;
    #500;
    sine_en = 1;
    #2000; // 10 cycles settle at 5 GHz
    vmax = -100.0; vmin = 100.0;
    for (i = 0; i < 4000; i = i + 1) begin // 20 cycles measured, 1 ps sampling
      #1;
      if (vout > vmax) vmax = vout;
      if (vout < vmin) vmin = vout;
    end
    sine_en = 0;
    vin = 0.0;
    amp     = (vmax - vmin) / 2.0;
    exp_amp = 0.1 * DC_GAIN / $sqrt(2.0); // -3 dB relative to DC gain
    if (amp < exp_amp*DB1_LO || amp > exp_amp*DB1_HI)
      fail($sformatf("pole-frequency response out of -3dB+/-1dB: amp=%g expected=%g", amp, exp_amp));
    $display("TB: pole-frequency check ok, amp=%g V (expected %g V, -3 dB)", amp, exp_amp);

    // -------- Test 3: overdrive / saturation --------
    vin = 3.0 * VOUT_SAT / DC_GAIN; // 3x input needed for full output swing
    #2000;
    if (vout > VOUT_SAT * 1.05)
      fail($sformatf("positive saturation exceeded: vout=%g limit=%g", vout, VOUT_SAT));
    if (vout < VOUT_SAT * 0.90)
      fail($sformatf("output never reached positive saturation: vout=%g", vout));
    vin = -3.0 * VOUT_SAT / DC_GAIN;
    #2000;
    if (vout < -VOUT_SAT * 1.05)
      fail($sformatf("negative saturation exceeded: vout=%g limit=%g", vout, -VOUT_SAT));
    if (vout > -VOUT_SAT * 0.90)
      fail($sformatf("output never reached negative saturation: vout=%g", vout));
    $display("TB: saturation check ok, |vout| clamped at %g V", VOUT_SAT);

    $display("ARCH_TB_PASS");
    $finish;
  end

endmodule
