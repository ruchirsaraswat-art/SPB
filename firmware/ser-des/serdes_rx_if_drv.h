/*
 * serdes_rx_if_drv.h
 * ------------------
 * Register-layer driver for the ser-des PHY AFE<->controller interface
 * "serdes-rx-if": per-register accessors plus the power-up/power-down
 * sequences generated from the interface power_sequence / reset_sequence.
 *
 * Scope (v1, register layer only): no training/cal supervisors, no host API,
 * no interrupts, no field packing, no RTOS. C99, no dynamic allocation.
 * All hardware access goes through the fw_reg_read32/fw_reg_write32 port
 * layer below, so the same code runs against the host stub (reg_stub.c) and
 * a real target.
 */

#ifndef SERDES_RX_IF_DRV_H
#define SERDES_RX_IF_DRV_H

#include <stdint.h>
#include "serdes_rx_if_regs.h"

/* ---- port layer (declared here, DEFINED by the platform) ---------------
 * Host build: reg_stub.c defines these against an array-backed model.
 * Real target: a target port file must define them as MMIO accesses and a
 * timer-based microsecond delay - that needs the real target toolchain,
 * which is deliberately NOT assumed here (host gcc only). */
extern uint32_t fw_reg_read32(uint32_t addr);
extern void     fw_reg_write32(uint32_t addr, uint32_t v);
/* Delay port used for the settle waits and poll granularity of the
 * sequences. On the host stub this only advances virtual time. */
extern void     fw_delay_us(uint32_t us);

/* ---- error codes ------------------------------------------------------ */
#define SERDES_RX_IF_OK                     0
/* power_sequence step 3: pll_locked not seen within timeout -> ERROR */
#define SERDES_RX_IF_ERR_PLL_LOCK_TIMEOUT  (-1)
/* power_sequence step 5: cdr_locked not seen after all retries -> ERROR */
#define SERDES_RX_IF_ERR_CDR_LOCK_TIMEOUT  (-2)
#define SERDES_RX_IF_ERR_BAD_ARG           (-3)

/* ---- timing parameters ------------------------------------------------
 * Every hardware-dependent time value lives here - nothing hard-coded in
 * the sequence code. Defaults are seeded from the interface definition;
 * each field cites its source. */
typedef struct {
    uint32_t bias_settle_us;         /* power_sequence step 2: "20 us settle" */
    uint32_t pll_lock_timeout_us;    /* power_sequence step 3: "timeout 500 us -> ERROR" */
    uint32_t rx_fe_settle_us;        /* power_sequence step 4: "5 us settle" */
    uint32_t cdr_lock_timeout_us;    /* power_sequence step 5: "timeout 1 ms" */
    uint32_t cdr_retries;            /* power_sequence step 5: "retry from 4, 3x then ERROR" */
    uint32_t deser_release_word_clks;/* power_sequence step 6: "16 word clocks" */
    uint32_t word_clk_freq_mhz;      /* clock_domains[clk_rx_word].freq_mhz = 100 */
    uint32_t poll_interval_us;       /* firmware choice (poll granularity for
                                      * lock waits) - NOT a spec field; the
                                      * interface gives no poll-rate value. */
} serdes_rx_if_timing_t;

#define SERDES_RX_IF_TIMING_DEFAULT                        \
    {                                                      \
        20u,    /* bias_settle_us          (pwr step 2) */ \
        500u,   /* pll_lock_timeout_us     (pwr step 3) */ \
        5u,     /* rx_fe_settle_us         (pwr step 4) */ \
        1000u,  /* cdr_lock_timeout_us     (pwr step 5) */ \
        3u,     /* cdr_retries             (pwr step 5) */ \
        16u,    /* deser_release_word_clks (pwr step 6) */ \
        100u,   /* word_clk_freq_mhz (clk_rx_word)      */ \
        10u     /* poll_interval_us  (fw choice)        */ \
    }

/* Fill *t with the defaults above. */
void serdes_rx_if_timing_default(serdes_rx_if_timing_t *t);

/* ---- sequences --------------------------------------------------------
 * Generated from the interface power_sequence (steps 1-6; step 7, word
 * alignment/training, is digital-internal supervisor work and out of scope
 * for the register layer). t == NULL uses SERDES_RX_IF_TIMING_DEFAULT.
 * Returns SERDES_RX_IF_OK or a negative error code above. */
int serdes_rx_if_power_up(const serdes_rx_if_timing_t *t);

/* Reverse order of power-up: deser_rstn=0, en_cdr=0, en_rx_fe=0, en_pll=0,
 * en_afe_bias=0 ("first on, last off" per en_afe_bias meaning). */
int serdes_rx_if_power_down(void);

/* ---- per-register accessors -------------------------------------------
 * One get/set pair per RW (d2a) register, get-only for RO (a2d) status.
 * Names trace 1:1 to interface signal names. Setters mask the value to the
 * field width; getters return the field value (bits [width-1:0]). */

/* control (RW) */
void     serdes_rx_if_set_ctle_peak(uint32_t code);       /* ctle_peak      */
uint32_t serdes_rx_if_get_ctle_peak(void);
void     serdes_rx_if_set_vga_gain(uint32_t code);        /* vga_gain       */
uint32_t serdes_rx_if_get_vga_gain(void);
void     serdes_rx_if_set_dfe_tap1(uint32_t code);        /* dfe_tap1       */
uint32_t serdes_rx_if_get_dfe_tap1(void);
void     serdes_rx_if_set_slicer_ofst(uint32_t code);     /* slicer_ofst    */
uint32_t serdes_rx_if_get_slicer_ofst(void);
void     serdes_rx_if_set_eye_vref_code(uint32_t code);   /* eye_vref_code  */
uint32_t serdes_rx_if_get_eye_vref_code(void);
void     serdes_rx_if_set_eye_phase_code(uint32_t code);  /* eye_phase_code */
uint32_t serdes_rx_if_get_eye_phase_code(void);
void     serdes_rx_if_set_adapt_hold(uint32_t v);         /* adapt_hold     */
uint32_t serdes_rx_if_get_adapt_hold(void);

/* status (RO) */
uint32_t serdes_rx_if_get_pll_locked(void);               /* pll_locked     */
uint32_t serdes_rx_if_get_cdr_locked(void);               /* cdr_locked     */
uint32_t serdes_rx_if_get_sig_detect(void);               /* sig_detect     */

/* reset (RW) */
void     serdes_rx_if_set_deser_rstn(uint32_t v);         /* deser_rstn     */
uint32_t serdes_rx_if_get_deser_rstn(void);

/* power (RW) */
void     serdes_rx_if_set_en_afe_bias(uint32_t v);        /* en_afe_bias    */
uint32_t serdes_rx_if_get_en_afe_bias(void);
void     serdes_rx_if_set_en_pll(uint32_t v);             /* en_pll         */
uint32_t serdes_rx_if_get_en_pll(void);
void     serdes_rx_if_set_en_rx_fe(uint32_t v);           /* en_rx_fe       */
uint32_t serdes_rx_if_get_en_rx_fe(void);
void     serdes_rx_if_set_en_cdr(uint32_t v);             /* en_cdr         */
uint32_t serdes_rx_if_get_en_cdr(void);

#endif /* SERDES_RX_IF_DRV_H */
