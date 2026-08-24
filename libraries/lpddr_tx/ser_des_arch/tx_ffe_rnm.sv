// -----------------------------------------------------------------------------
// tx_ffe_rnm.sv -- RNM behavioral model of a TX feed-forward equalizer
//                  (pre-emphasis) for a generic ser-des PHY.
//
// Topology: FFE (UI-spaced FIR). On every rising edge of the UI bit clock the
// input sample is shifted into a real-valued delay line and the output is
//     dout = sum_i ( TAPS[i] * x[n-i] )
// i.e. TAPS[0] is the pre-cursor (newest sample), TAPS[NUM_TAPS-1] the last
// post-cursor. Purely event-driven: no continuous dynamics, so no internal
// sample tick is needed (FIR state only changes on bit-clock edges).
//
// This is a TX FFE (topology "ffe"): no comparator / decision feedback is
// modeled here -- DFE belongs to an RX equalizer block, not present in this
// run's block selection.
//
// Signal convention (directional RNM): real-valued data in / real-valued data
// out; `clk` is the UI-rate bit clock supplied by the upstream serializer/PLL
// (external in this run). Outputs tell the next block a value only -- no
// impedance / bidirectional termination or reflection modeling at this level.
//
// Engineering-judgment defaults (no per-block specs entered for this run):
//   NUM_TAPS = 3                  -- pre / main / post, the common TX FFE
//   TAPS     = {-0.10, 0.75, -0.15}  -- mild pre-emphasis; sum(|tap|) = 1.0
//                                    so peak output swing is normalized to
//                                    the +/-1.0 input swing convention
//   (UI itself is set by the external bit clock; nominal chain rate is
//    10 Gb/s -> UI = 100 ps, consistent with the testbench.)
//
// Tool note: the taps live in an internal real array `taps[]` (UI-spaced FIR
// per the modeling spec). Icarus Verilog 12 rejects unpacked-array *module
// parameters*, so the array is initialized at time 0 from the scalar real
// parameters TAP_PRE/TAP_MAIN/TAP_POST instead of a parameter array literal.
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module tx_ffe_rnm #(
    parameter int  NUM_TAPS = 3,
    parameter real TAP_PRE  = -0.10,  // taps[0], pre-cursor
    parameter real TAP_MAIN =  0.75,  // taps[1], main cursor
    parameter real TAP_POST = -0.15   // taps[2], post-cursor
) (
    input  wire clk,    // UI-rate bit clock (from serializer/PLL, external here)
    input  real din,    // TX data, real-valued (+1.0 / -1.0 NRZ convention)
    output real dout    // equalized (pre-emphasized) TX data
);

    real taps  [0:NUM_TAPS-1];  // real tap-weight array (see tool note above)
    real dline [0:NUM_TAPS-1];  // dline[0] = newest sample x[n]
    real acc;
    integer i;

    initial begin
        taps[0] = TAP_PRE;
        taps[1] = TAP_MAIN;
        taps[2] = TAP_POST;
        for (i = 0; i < NUM_TAPS; i = i + 1) dline[i] = 0.0;
        dout = 0.0;
    end

    always @(posedge clk) begin
        // shift UI-spaced delay line
        for (i = NUM_TAPS-1; i > 0; i = i - 1) dline[i] = dline[i-1];
        dline[0] = din;
        // FIR sum
        acc = 0.0;
        for (i = 0; i < NUM_TAPS; i = i + 1) acc = acc + taps[i]*dline[i];
        dout = acc;
    end

endmodule
