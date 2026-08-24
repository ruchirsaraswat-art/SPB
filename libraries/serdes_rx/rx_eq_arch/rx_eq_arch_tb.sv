// -----------------------------------------------------------------------------
// rx_eq_arch_tb.sv - self-checking testbench for rx_eq_arch (RNM level)
//
// Checks (against rx_eq_rnm defaults: A0 = 4.0, f_pole = 5 GHz, VSAT = 0.9 V):
//   1. DC gain: drive 0.1 V DC, settle >> tau, |gain| within 1 dB of A0.
//   2. Pole-frequency response: drive a 5 GHz sine (amplitude 0.05 V, linear
//      region) for 30 cycles, measure steady-state peak over the last 10
//      cycles; amplitude ratio must be A0/sqrt(2) within +/-1 dB. This catches
//      sample-tick aliasing (no FFT - direct amplitude-ratio measurement).
//      Note f_pole = Nyquist for the 10 Gb/s default, so this is also the
//      mandatory Nyquist response check.
//   3. Overdrive/saturation: drive 3x the input that gives full swing at the
//      set gain (3 * VSAT/A0 = 0.675 V); |out| must stay <= VSAT within 5%,
//      and must actually reach near the limit (sanity that drive was large).
//   4. Liveness: output must move during the sine test (no stuck output).
//   5. Global watchdog: fails and exits if simulation hangs.
//
// Prints exactly one of ARCH_TB_PASS / ARCH_TB_FAIL <reason>.
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module rx_eq_arch_tb;

    // Must match the DUT defaults
    localparam real A0      = 4.0;
    localparam real FPOLE   = 5.0e9;
    localparam real VSAT    = 0.9;
    localparam real PI      = 3.14159265358979;
    localparam real UDB     = 1.1220184543;  // 10^(1/20), +/-1 dB ratio

    localparam real SINE_AMP  = 0.05;        // V, stays linear at the output
    localparam real DC_IN     = 0.1;         // V, DC gain test input
    localparam real OVR_IN    = 3.0*VSAT/A0; // 0.675 V, 3x full-swing input
    localparam real T_CYC_PS  = 1.0e12/FPOLE;      // 200 ps sine period
    localparam real WATCHDOG_PS = 300000.0;  // ~20x expected ~15 ns test time

    real in_v;
    real out_v;
    integer n_fail;
    integer sine_en;
    real out_pk;      // peak |out| in sine measurement window
    real out_min_w;   // min out in window (liveness)
    real out_max_w;   // max out in window
    real ratio, dc_gain;
    integer i;

    rx_eq_arch dut (
        .in_v  (in_v),
        .out_v (out_v)
    );

    // ---- global watchdog (mandatory) ----
    initial begin
        #(WATCHDOG_PS);
        $display("RNM_TB_FAIL watchdog timeout");
        $display("ARCH_TB_FAIL watchdog timeout");
        $finish;
    end

    // ---- sine source + peak detector, 1 ps resolution ----
    always #1 begin
        if (sine_en != 0) begin
            in_v = SINE_AMP * $sin(2.0*PI*FPOLE*($realtime*1.0e-12));
            if (out_v >  out_max_w) out_max_w = out_v;
            if (out_v <  out_min_w) out_min_w = out_v;
        end
    end

    initial begin
        n_fail  = 0;
        sine_en = 0;
        in_v    = 0.0;
        out_max_w = -1.0e9;
        out_min_w =  1.0e9;

        // ---------- Test 1: DC gain ----------
        in_v = DC_IN;
        #3000; // 3 ns >> tau (~32 ps)
        dc_gain = out_v / DC_IN;
        $display("TB: DC gain measured = %f (expected %f, +/-1 dB)", dc_gain, A0);
        if (!(dc_gain > A0/UDB && dc_gain < A0*UDB)) begin
            $display("ARCH_TB_FAIL dc gain %f outside 1 dB of %f", dc_gain, A0);
            n_fail = n_fail + 1;
        end

        // ---------- Test 2: response at the pole frequency ----------
        in_v = 0.0;
        #1000; // let state decay
        sine_en = 1;
        // 20 cycles to reach steady state (discard), then 10-cycle window
        #(20.0*T_CYC_PS);
        out_max_w = -1.0e9;
        out_min_w =  1.0e9;
        #(10.0*T_CYC_PS);
        sine_en = 0;
        in_v = 0.0;
        out_pk = (out_max_w > -out_min_w) ? out_max_w : -out_min_w;
        ratio  = out_pk / SINE_AMP;
        $display("TB: pole-freq amplitude ratio = %f (expected %f, +/-1 dB)",
                 ratio, A0/1.41421356);
        if (!(ratio > (A0/1.41421356)/UDB && ratio < (A0/1.41421356)*UDB)) begin
            $display("ARCH_TB_FAIL pole-frequency response ratio %f outside 1 dB of %f",
                     ratio, A0/1.41421356);
            n_fail = n_fail + 1;
        end
        // Liveness: output must have moved during the sine test
        if (!(out_max_w > out_min_w + 0.01)) begin
            $display("ARCH_TB_FAIL output stuck during sine test (max %f min %f)",
                     out_max_w, out_min_w);
            n_fail = n_fail + 1;
        end

        // ---------- Test 3: overdrive / saturation ----------
        in_v = OVR_IN; // 3x the full-swing input at the set gain
        #3000;
        // sample |out| over a window to catch any excursion above the limit
        out_pk = 0.0;
        for (i = 0; i < 500; i = i + 1) begin
            #2;
            if (out_v > out_pk)  out_pk = out_v;
            if (-out_v > out_pk) out_pk = -out_v;
        end
        $display("TB: overdrive |out| peak = %f (limit %f +5%% = %f)",
                 out_pk, VSAT, VSAT*1.05);
        if (!(out_pk <= VSAT*1.05)) begin
            $display("ARCH_TB_FAIL saturation violated: |out| %f > %f",
                     out_pk, VSAT*1.05);
            n_fail = n_fail + 1;
        end
        if (!(out_pk >= 0.8*VSAT)) begin
            $display("ARCH_TB_FAIL overdrive did not reach saturation region: |out| %f < %f",
                     out_pk, 0.8*VSAT);
            n_fail = n_fail + 1;
        end

        // ---------- verdict ----------
        if (n_fail == 0)
            $display("ARCH_TB_PASS");
        $finish;
    end

endmodule
