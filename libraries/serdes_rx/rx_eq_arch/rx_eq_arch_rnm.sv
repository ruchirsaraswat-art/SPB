// -----------------------------------------------------------------------------
// rx_eq_arch_rnm.sv - top-level architecture model, ser-des PHY (1 block run)
//
// Inter-block signal convention (RNM): every diagram edge is a single `real`
// data signal carrying a voltage in volts; signal flow is strictly directional
// (driver reports a value, receiver presents no load). Clock-generating blocks
// would drive an event/bit clock signal - none are included in this run.
//
// This run includes only the `rx-eq` block (rx_eq_rnm). There are no edges
// between included blocks, so the single instance is placed with the chain's
// primary input and output exposed as top-level real ports:
//   in_v  -> rx_eq_rnm.in_v   (chain input,  from the excluded rx-frontend)
//   out_v <- rx_eq_rnm.out_v  (chain output, toward the excluded sampler)
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module rx_eq_arch (
    input  real in_v,   // chain primary input (equalizer input voltage)
    output real out_v   // chain primary output (equalized voltage)
);

    rx_eq_rnm u_rx_eq (
        .in_v  (in_v),
        .out_v (out_v)
    );

endmodule
