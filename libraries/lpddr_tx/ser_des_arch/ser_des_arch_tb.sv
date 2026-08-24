// -----------------------------------------------------------------------------
// ser_des_arch_tb.sv -- self-checking testbench for ser_des_arch (RNM).
//
// Tests (known-answer):
//   1) Impulse response: drive a single +1.0 sample (one UI wide) through the
//      chain; the FFE output must read back the tap weights exactly, one per
//      UI: -0.10, 0.75, -0.15, then 0.0 (delay-line flushed).
//   2) Step/DC check: drive constant +1.0 for many UIs; steady-state output
//      must equal sum(taps) = 0.50 (output responds to input, not stuck).
//   3) No-X check is implicit: `real` signals cannot be X, but we verify the
//      output is finite and matches expectations to 1e-9.
// Mandatory global watchdog: prints RNM_TB_FAIL token and finishes if the
// test hangs (~20x expected duration).
//
// Prints exactly one of: ARCH_TB_PASS / ARCH_TB_FAIL <reason>
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module ser_des_arch_tb;

    // Chain default: 10 Gb/s -> UI = 100 ps
    localparam real UI_PS      = 100.0;
    localparam real TOL        = 1e-9;
    localparam int  WATCHDOG_PS = 100000;   // ~20x the ~4 ns test duration

    // Expected tap values (must match tx_ffe_rnm defaults)
    localparam real TAP0 = -0.10;
    localparam real TAP1 =  0.75;
    localparam real TAP2 = -0.15;
    localparam real DCGAIN = TAP0 + TAP1 + TAP2;  // 0.50

    reg  bit_clk;
    real tx_in;
    real tx_out;

    integer errors;

    ser_des_arch dut (
        .bit_clk (bit_clk),
        .tx_in   (tx_in),
        .tx_out  (tx_out)
    );

    // UI-rate bit clock
    initial bit_clk = 1'b0;
    always #(UI_PS/2.0) bit_clk = ~bit_clk;

    // Mandatory global watchdog
    initial begin
        #WATCHDOG_PS;
        $display("RNM_TB_FAIL watchdog timeout");
        $display("ARCH_TB_FAIL watchdog timeout");
        $finish;
    end

    task check(input real got, input real exp, input string what);
        begin
            if ((got > exp + TOL) || (got < exp - TOL)) begin
                errors = errors + 1;
                $display("  MISMATCH %s: got %0.9f expected %0.9f", what, got, exp);
            end else begin
                $display("  ok %s: %0.9f", what, got);
            end
        end
    endtask

    integer k;

    initial begin
        errors = 0;
        tx_in  = 0.0;

        // let delay line flush with zeros
        repeat (4) @(posedge bit_clk);

        // ---- Test 1: impulse -> tap weights read back exactly ----
        $display("Test 1: FFE impulse response (tap read-back)");
        @(negedge bit_clk) tx_in = 1.0;     // one UI-wide impulse
        @(posedge bit_clk); #1; check(tx_out, TAP0, "tap0 (pre)");
        @(negedge bit_clk) tx_in = 0.0;
        @(posedge bit_clk); #1; check(tx_out, TAP1, "tap1 (main)");
        @(posedge bit_clk); #1; check(tx_out, TAP2, "tap2 (post)");
        @(posedge bit_clk); #1; check(tx_out, 0.0,  "flush (zero)");

        // ---- Test 2: DC step -> steady state = sum(taps) ----
        $display("Test 2: DC step response (output responds, steady = sum(taps))");
        @(negedge bit_clk) tx_in = 1.0;
        repeat (6) @(posedge bit_clk);      // > NUM_TAPS UIs to settle
        #1; check(tx_out, DCGAIN, "DC gain sum(taps)");
        @(negedge bit_clk) tx_in = -1.0;
        repeat (6) @(posedge bit_clk);
        #1; check(tx_out, -DCGAIN, "DC gain (negative input)");

        if (errors == 0) $display("ARCH_TB_PASS");
        else             $display("ARCH_TB_FAIL %0d mismatches", errors);
        $finish;
    end

endmodule
