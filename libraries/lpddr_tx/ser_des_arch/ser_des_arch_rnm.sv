// -----------------------------------------------------------------------------
// ser_des_arch_rnm.sv -- top-level RNM architecture model of the ser-des PHY
//                        block diagram for this run.
//
// Blocks included in this run: tx-ffe only (all other diagram blocks --
// serializer, tx-driver, pads, rx front-end/eq/sampler, deserializer, PLL,
// CDR -- were not selected). There are therefore no inter-block edges to
// wire: the single block is instantiated and its natural ports are exposed
// directly as the chain's primary input(s) and output(s).
//
// Inter-block signal convention (documented per wiring rules, applied to the
// exposed ports since no internal edges exist):
//   * one `real` data signal per diagram edge, directional flow only
//     (a value is told to the next block; no impedance / reflections);
//   * clock-generating blocks drive a plain event/bit clock (`wire`) to the
//     blocks they feed. The tx-ffe's UI bit clock would come from the
//     serializer/PLL in the full diagram; since neither is included, the
//     bit clock is exposed as the top-level input `bit_clk` (documented
//     judgment call rather than inventing an internal clock source).
//
// Chain-wide defaults: 10 Gb/s data rate -> UI = 100 ps bit clock, data
// represented as real +/-1.0 NRZ levels. FFE taps default to
// {-0.10, 0.75, -0.15} (see tx_ffe_rnm.sv header).
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module ser_des_arch (
    input  wire bit_clk,   // UI-rate bit clock (10 GHz nominal; external here)
    input  real tx_in,     // chain primary input: raw TX data, real +/-1.0
    output real tx_out     // chain primary output: pre-emphasized TX data
);

    tx_ffe_rnm u_tx_ffe (
        .clk  (bit_clk),
        .din  (tx_in),
        .dout (tx_out)
    );

endmodule
