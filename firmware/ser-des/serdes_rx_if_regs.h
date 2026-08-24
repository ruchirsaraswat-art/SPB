/*
 * serdes_rx_if_regs.h
 * -------------------
 * Register map for the ser-des PHY AFE<->controller interface "serdes-rx-if".
 *
 * GENERATED-STYLE FILE - source of truth is the interface definition
 * (analog-spec-tool, interface "serdes-rx-if", phy_type "ser-des").
 * Do not hand-edit field definitions; regenerate from the interface JSON.
 *
 * Codegen rule (deterministic):
 *   - Signals with group in {control, status, reset, power} and direction in
 *     {d2a, a2d} get one 32-bit register each.
 *   - Order: control -> status -> reset -> power (interface table order
 *     within a group); offset = 4 * index.
 *   - Field sits at bits [width-1:0] of its register.
 *   - d2a signals are RW, a2d signals are RO (writes ignored).
 *   - Excluded (NOT CSR-reachable): ext-direction signals (clk_ref,
 *     rstn_por), clock-group signals (clk_rx_word), and word-rate data buses
 *     (rx_pdata, rx_err_sample, rx_err_valid).
 *
 * Name mapping: interface signal <name> -> SERDES_RX_IF_<NAME>_* macros.
 */

#ifndef SERDES_RX_IF_REGS_H
#define SERDES_RX_IF_REGS_H

#define SERDES_RX_IF_BASE_ADDR 0x40010000u

#define SERDES_RX_IF_NUM_REGS  15u

/* ---- control group ---------------------------------------------------- */

/* signal: ctle_peak (d2a, width 4) - access: RW
 * CTLE peaking code (0-15). Hold rule: change only while adapt_hold=1 or
 * en_rx_fe=0. */
#define SERDES_RX_IF_CTLE_PEAK_OFFSET       0x0000u
#define SERDES_RX_IF_CTLE_PEAK_SHIFT        0u
#define SERDES_RX_IF_CTLE_PEAK_WIDTH        4u
#define SERDES_RX_IF_CTLE_PEAK_MASK         0x0000000Fu

/* signal: vga_gain (d2a, width 5) - access: RW
 * VGA gain code (0-31). Same hold rule as ctle_peak. */
#define SERDES_RX_IF_VGA_GAIN_OFFSET        0x0004u
#define SERDES_RX_IF_VGA_GAIN_SHIFT         0u
#define SERDES_RX_IF_VGA_GAIN_WIDTH         5u
#define SERDES_RX_IF_VGA_GAIN_MASK          0x0000001Fu

/* signal: dfe_tap1 (d2a, width 6) - access: RW
 * DFE tap-1 weight, sign-magnitude (bit5 = sign). */
#define SERDES_RX_IF_DFE_TAP1_OFFSET        0x0008u
#define SERDES_RX_IF_DFE_TAP1_SHIFT         0u
#define SERDES_RX_IF_DFE_TAP1_WIDTH         6u
#define SERDES_RX_IF_DFE_TAP1_MASK          0x0000003Fu

/* signal: slicer_ofst (d2a, width 6) - access: RW
 * Data-slicer offset trim, sign-magnitude, ~1 mV/LSB. */
#define SERDES_RX_IF_SLICER_OFST_OFFSET     0x000Cu
#define SERDES_RX_IF_SLICER_OFST_SHIFT      0u
#define SERDES_RX_IF_SLICER_OFST_WIDTH      6u
#define SERDES_RX_IF_SLICER_OFST_MASK       0x0000003Fu

/* signal: eye_vref_code (d2a, width 6) - access: RW
 * Error-slicer threshold DAC code (eye monitor vertical axis). */
#define SERDES_RX_IF_EYE_VREF_CODE_OFFSET   0x0010u
#define SERDES_RX_IF_EYE_VREF_CODE_SHIFT    0u
#define SERDES_RX_IF_EYE_VREF_CODE_WIDTH    6u
#define SERDES_RX_IF_EYE_VREF_CODE_MASK     0x0000003Fu

/* signal: eye_phase_code (d2a, width 6) - access: RW
 * PI code for error-slicer sampling phase (64 steps/UI). */
#define SERDES_RX_IF_EYE_PHASE_CODE_OFFSET  0x0014u
#define SERDES_RX_IF_EYE_PHASE_CODE_SHIFT   0u
#define SERDES_RX_IF_EYE_PHASE_CODE_WIDTH   6u
#define SERDES_RX_IF_EYE_PHASE_CODE_MASK    0x0000003Fu

/* signal: adapt_hold (d2a, width 1) - access: RW
 * Freeze AFE-internal adaptation while digital rewrites trim codes. */
#define SERDES_RX_IF_ADAPT_HOLD_OFFSET      0x0018u
#define SERDES_RX_IF_ADAPT_HOLD_SHIFT       0u
#define SERDES_RX_IF_ADAPT_HOLD_WIDTH       1u
#define SERDES_RX_IF_ADAPT_HOLD_MASK        0x00000001u

/* ---- status group ----------------------------------------------------- */

/* signal: pll_locked (a2d, width 1) - access: RO (writes ignored)
 * PLL lock detector; gates power-up sequencing step 3. 2ff-synced. */
#define SERDES_RX_IF_PLL_LOCKED_OFFSET      0x001Cu
#define SERDES_RX_IF_PLL_LOCKED_SHIFT       0u
#define SERDES_RX_IF_PLL_LOCKED_WIDTH       1u
#define SERDES_RX_IF_PLL_LOCKED_MASK        0x00000001u

/* signal: cdr_locked (a2d, width 1) - access: RO (writes ignored)
 * CDR lock/valid-data; gates deser reset release. 2ff-synced. */
#define SERDES_RX_IF_CDR_LOCKED_OFFSET      0x0020u
#define SERDES_RX_IF_CDR_LOCKED_SHIFT       0u
#define SERDES_RX_IF_CDR_LOCKED_WIDTH       1u
#define SERDES_RX_IF_CDR_LOCKED_MASK        0x00000001u

/* signal: sig_detect (a2d, width 1) - access: RO (writes ignored)
 * Input amplitude above threshold (loss-of-signal inverse). 2ff-synced. */
#define SERDES_RX_IF_SIG_DETECT_OFFSET      0x0024u
#define SERDES_RX_IF_SIG_DETECT_SHIFT       0u
#define SERDES_RX_IF_SIG_DETECT_WIDTH      1u
#define SERDES_RX_IF_SIG_DETECT_MASK        0x00000001u

/* ---- reset group ------------------------------------------------------ */

/* signal: deser_rstn (d2a, width 1) - access: RW
 * Deserializer/word-counter reset: async assert, release synchronized into
 * clk_rx_word by the AFE. Release only after cdr_locked (power seq step 6). */
#define SERDES_RX_IF_DESER_RSTN_OFFSET      0x0028u
#define SERDES_RX_IF_DESER_RSTN_SHIFT       0u
#define SERDES_RX_IF_DESER_RSTN_WIDTH       1u
#define SERDES_RX_IF_DESER_RSTN_MASK        0x00000001u

/* ---- power group ------------------------------------------------------ */

/* signal: en_afe_bias (d2a, width 1) - access: RW
 * Master bias enable (bandgap, bias mirrors). First on, last off. */
#define SERDES_RX_IF_EN_AFE_BIAS_OFFSET     0x002Cu
#define SERDES_RX_IF_EN_AFE_BIAS_SHIFT      0u
#define SERDES_RX_IF_EN_AFE_BIAS_WIDTH      1u
#define SERDES_RX_IF_EN_AFE_BIAS_MASK       0x00000001u

/* signal: en_pll (d2a, width 1) - access: RW
 * PLL enable. */
#define SERDES_RX_IF_EN_PLL_OFFSET          0x0030u
#define SERDES_RX_IF_EN_PLL_SHIFT           0u
#define SERDES_RX_IF_EN_PLL_WIDTH           1u
#define SERDES_RX_IF_EN_PLL_MASK            0x00000001u

/* signal: en_rx_fe (d2a, width 1) - access: RW
 * RX frontend enable (term/VGA/CTLE/DFE/slicer). */
#define SERDES_RX_IF_EN_RX_FE_OFFSET        0x0034u
#define SERDES_RX_IF_EN_RX_FE_SHIFT         0u
#define SERDES_RX_IF_EN_RX_FE_WIDTH         1u
#define SERDES_RX_IF_EN_RX_FE_MASK          0x00000001u

/* signal: en_cdr (d2a, width 1) - access: RW
 * CDR enable. */
#define SERDES_RX_IF_EN_CDR_OFFSET          0x0038u
#define SERDES_RX_IF_EN_CDR_SHIFT           0u
#define SERDES_RX_IF_EN_CDR_WIDTH           1u
#define SERDES_RX_IF_EN_CDR_MASK            0x00000001u

#endif /* SERDES_RX_IF_REGS_H */
