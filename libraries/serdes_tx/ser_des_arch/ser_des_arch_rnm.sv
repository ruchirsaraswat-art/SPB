// -----------------------------------------------------------------------------
// ser_des_arch_rnm.sv - top-level ser-des PHY architecture model (RNM)
// -----------------------------------------------------------------------------
// Author: Circuit_Builder (for ruchir.saraswat@gmail.com)
//
// Inter-block signal convention (applies chain-wide):
//   - Every diagram data edge is ONE SystemVerilog `real` signal carrying the
//     instantaneous single-ended-equivalent voltage of that node, directional
//     source -> sink (no impedance/reflection modeling).
//   - Clock-generating blocks (PLL/CDR - not included in this run) would drive
//     an event/bit clock signal (`logic` edge) to the blocks they feed.
//
// Blocks included in this run: rx-eq only. The diagram has no edges between
// included blocks, so rx_eq_rnm is instantiated standalone and its natural
// ports are promoted directly to the top level:
//   rx_in_v  -> rx_eq vin   (chain primary input)
//   rx_out_v <- rx_eq vout  (chain primary output)
// All other diagram blocks (rx-pad, rx-frontend, sampler, deserializer, pll,
// cdr, tx-*, serializer) are outside this run's scope.
//
// Chain-wide defaults: 10 Gb/s NRZ (UI = 100 ps), 1.8 V supply class.
// -----------------------------------------------------------------------------
`timescale 1ps/1fs

module ser_des_arch (
  input  real rx_in_v,   // RX equalizer input voltage
  output real rx_out_v   // RX equalizer output voltage
);

  rx_eq_rnm u_rx_eq (
    .vin  (rx_in_v),
    .vout (rx_out_v)
  );

endmodule
