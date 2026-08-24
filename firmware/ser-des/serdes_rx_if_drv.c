/*
 * serdes_rx_if_drv.c
 * ------------------
 * Register-layer driver implementation for interface "serdes-rx-if".
 * See serdes_rx_if_drv.h for scope and the port-layer contract.
 */

#include "serdes_rx_if_drv.h"

#include <stddef.h> /* NULL */

/* ---- internal helpers ------------------------------------------------- */

static uint32_t reg_addr(uint32_t offset)
{
    return SERDES_RX_IF_BASE_ADDR + offset;
}

static void field_write(uint32_t offset, uint32_t mask, uint32_t shift,
                        uint32_t v)
{
    /* One field per register at bits [width-1:0] (codegen rule), so a plain
     * masked write is exact - no read-modify-write needed in v1. */
    fw_reg_write32(reg_addr(offset), (v << shift) & mask);
}

static uint32_t field_read(uint32_t offset, uint32_t mask, uint32_t shift)
{
    return (fw_reg_read32(reg_addr(offset)) & mask) >> shift;
}

/* Poll a 1-bit RO status field until it reads 1 or timeout_us elapses.
 * Returns 0 on lock, -1 on timeout. Poll granularity = poll_interval_us. */
static int poll_status_set(uint32_t offset, uint32_t mask, uint32_t shift,
                           uint32_t timeout_us, uint32_t poll_interval_us)
{
    uint32_t elapsed_us = 0u;
    uint32_t step_us = (poll_interval_us == 0u) ? 1u : poll_interval_us;

    for (;;) {
        if (field_read(offset, mask, shift) != 0u) {
            return 0;
        }
        if (elapsed_us >= timeout_us) {
            return -1;
        }
        fw_delay_us(step_us);
        elapsed_us += step_us;
    }
}

/* ---- timing defaults -------------------------------------------------- */

void serdes_rx_if_timing_default(serdes_rx_if_timing_t *t)
{
    const serdes_rx_if_timing_t def = SERDES_RX_IF_TIMING_DEFAULT;
    if (t != NULL) {
        *t = def;
    }
}

/* ---- power-up sequence ------------------------------------------------
 * Generated from interface power_sequence steps 1-6 (step 7 = word
 * alignment / training is digital-internal supervisor code, out of scope
 * for the register layer). Retry annotation on step 5 becomes a bounded
 * retry loop back to step 4. */
int serdes_rx_if_power_up(const serdes_rx_if_timing_t *t)
{
    serdes_rx_if_timing_t def;
    uint32_t attempt;

    if (t == NULL) {
        serdes_rx_if_timing_default(&def);
        t = &def;
    }

    /* Step 1: "POR released, all enables 0, deser_rstn=0".
     * Drive the documented safe state explicitly (reverse of power-up order,
     * matching power-down) so the sequence is re-entrant after a partial
     * bring-up. */
    serdes_rx_if_set_deser_rstn(0u);
    serdes_rx_if_set_en_cdr(0u);
    serdes_rx_if_set_en_rx_fe(0u);
    serdes_rx_if_set_en_pll(0u);
    serdes_rx_if_set_en_afe_bias(0u);

    /* Step 2: "Enable bias (en_afe_bias=1)", wait_for "20 us settle". */
    serdes_rx_if_set_en_afe_bias(1u);
    fw_delay_us(t->bias_settle_us);

    /* Step 3: "Enable PLL (en_pll=1)",
     * wait_for "pll_locked (timeout 500 us -> ERROR)". */
    serdes_rx_if_set_en_pll(1u);
    if (poll_status_set(SERDES_RX_IF_PLL_LOCKED_OFFSET,
                        SERDES_RX_IF_PLL_LOCKED_MASK,
                        SERDES_RX_IF_PLL_LOCKED_SHIFT,
                        t->pll_lock_timeout_us,
                        t->poll_interval_us) != 0) {
        return SERDES_RX_IF_ERR_PLL_LOCK_TIMEOUT;
    }

    /* Steps 4-5 with the step-5 retry annotation:
     * "cdr_locked (timeout 1 ms -> retry from 4, 3x then ERROR)"
     * => 1 initial attempt + cdr_retries retries. */
    for (attempt = 0u; attempt <= t->cdr_retries; attempt++) {
        /* Step 4: "Enable RX frontend (en_rx_fe=1)", wait_for "5 us settle". */
        serdes_rx_if_set_en_rx_fe(1u);
        fw_delay_us(t->rx_fe_settle_us);

        /* Step 5: "Enable CDR (en_cdr=1)", wait_for cdr_locked. */
        serdes_rx_if_set_en_cdr(1u);
        if (poll_status_set(SERDES_RX_IF_CDR_LOCKED_OFFSET,
                            SERDES_RX_IF_CDR_LOCKED_MASK,
                            SERDES_RX_IF_CDR_LOCKED_SHIFT,
                            t->cdr_lock_timeout_us,
                            t->poll_interval_us) == 0) {
            break; /* locked */
        }
        /* Timed out: undo steps 5 and 4, then retry from step 4 (bounded). */
        serdes_rx_if_set_en_cdr(0u);
        serdes_rx_if_set_en_rx_fe(0u);
        if (attempt == t->cdr_retries) {
            return SERDES_RX_IF_ERR_CDR_LOCK_TIMEOUT;
        }
    }

    /* Step 6: "Release deserializer (deser_rstn=1)",
     * wait_for "16 word clocks". Converted to microseconds using the
     * clk_rx_word domain frequency (clock_domains: 100 MHz), rounded up. */
    serdes_rx_if_set_deser_rstn(1u);
    {
        uint32_t mhz = (t->word_clk_freq_mhz == 0u) ? 1u
                                                    : t->word_clk_freq_mhz;
        uint32_t wait_us = (t->deser_release_word_clks + mhz - 1u) / mhz;
        fw_delay_us(wait_us);
    }

    /* Step 7 ("Start word alignment / training") is digital-internal and
     * belongs to the (out-of-scope) training supervisor, not this layer. */
    return SERDES_RX_IF_OK;
}

/* ---- power-down sequence ----------------------------------------------
 * power_sequence step 7 note: "Power-down is the reverse order"; en_afe_bias
 * is "first on, last off". Sleep (steps 5-7 undone only) is a supervisor
 * policy decision - out of scope here. */
int serdes_rx_if_power_down(void)
{
    serdes_rx_if_set_deser_rstn(0u);  /* undo step 6 */
    serdes_rx_if_set_en_cdr(0u);      /* undo step 5 */
    serdes_rx_if_set_en_rx_fe(0u);    /* undo step 4 */
    serdes_rx_if_set_en_pll(0u);      /* undo step 3 */
    serdes_rx_if_set_en_afe_bias(0u); /* undo step 2 - last off */
    return SERDES_RX_IF_OK;
}

/* ---- per-register accessors ------------------------------------------- */

/* control (RW) */
void serdes_rx_if_set_ctle_peak(uint32_t code)
{
    field_write(SERDES_RX_IF_CTLE_PEAK_OFFSET, SERDES_RX_IF_CTLE_PEAK_MASK,
                SERDES_RX_IF_CTLE_PEAK_SHIFT, code);
}

uint32_t serdes_rx_if_get_ctle_peak(void)
{
    return field_read(SERDES_RX_IF_CTLE_PEAK_OFFSET,
                      SERDES_RX_IF_CTLE_PEAK_MASK,
                      SERDES_RX_IF_CTLE_PEAK_SHIFT);
}

void serdes_rx_if_set_vga_gain(uint32_t code)
{
    field_write(SERDES_RX_IF_VGA_GAIN_OFFSET, SERDES_RX_IF_VGA_GAIN_MASK,
                SERDES_RX_IF_VGA_GAIN_SHIFT, code);
}

uint32_t serdes_rx_if_get_vga_gain(void)
{
    return field_read(SERDES_RX_IF_VGA_GAIN_OFFSET,
                      SERDES_RX_IF_VGA_GAIN_MASK,
                      SERDES_RX_IF_VGA_GAIN_SHIFT);
}

void serdes_rx_if_set_dfe_tap1(uint32_t code)
{
    field_write(SERDES_RX_IF_DFE_TAP1_OFFSET, SERDES_RX_IF_DFE_TAP1_MASK,
                SERDES_RX_IF_DFE_TAP1_SHIFT, code);
}

uint32_t serdes_rx_if_get_dfe_tap1(void)
{
    return field_read(SERDES_RX_IF_DFE_TAP1_OFFSET,
                      SERDES_RX_IF_DFE_TAP1_MASK,
                      SERDES_RX_IF_DFE_TAP1_SHIFT);
}

void serdes_rx_if_set_slicer_ofst(uint32_t code)
{
    field_write(SERDES_RX_IF_SLICER_OFST_OFFSET,
                SERDES_RX_IF_SLICER_OFST_MASK,
                SERDES_RX_IF_SLICER_OFST_SHIFT, code);
}

uint32_t serdes_rx_if_get_slicer_ofst(void)
{
    return field_read(SERDES_RX_IF_SLICER_OFST_OFFSET,
                      SERDES_RX_IF_SLICER_OFST_MASK,
                      SERDES_RX_IF_SLICER_OFST_SHIFT);
}

void serdes_rx_if_set_eye_vref_code(uint32_t code)
{
    field_write(SERDES_RX_IF_EYE_VREF_CODE_OFFSET,
                SERDES_RX_IF_EYE_VREF_CODE_MASK,
                SERDES_RX_IF_EYE_VREF_CODE_SHIFT, code);
}

uint32_t serdes_rx_if_get_eye_vref_code(void)
{
    return field_read(SERDES_RX_IF_EYE_VREF_CODE_OFFSET,
                      SERDES_RX_IF_EYE_VREF_CODE_MASK,
                      SERDES_RX_IF_EYE_VREF_CODE_SHIFT);
}

void serdes_rx_if_set_eye_phase_code(uint32_t code)
{
    field_write(SERDES_RX_IF_EYE_PHASE_CODE_OFFSET,
                SERDES_RX_IF_EYE_PHASE_CODE_MASK,
                SERDES_RX_IF_EYE_PHASE_CODE_SHIFT, code);
}

uint32_t serdes_rx_if_get_eye_phase_code(void)
{
    return field_read(SERDES_RX_IF_EYE_PHASE_CODE_OFFSET,
                      SERDES_RX_IF_EYE_PHASE_CODE_MASK,
                      SERDES_RX_IF_EYE_PHASE_CODE_SHIFT);
}

void serdes_rx_if_set_adapt_hold(uint32_t v)
{
    field_write(SERDES_RX_IF_ADAPT_HOLD_OFFSET, SERDES_RX_IF_ADAPT_HOLD_MASK,
                SERDES_RX_IF_ADAPT_HOLD_SHIFT, v);
}

uint32_t serdes_rx_if_get_adapt_hold(void)
{
    return field_read(SERDES_RX_IF_ADAPT_HOLD_OFFSET,
                      SERDES_RX_IF_ADAPT_HOLD_MASK,
                      SERDES_RX_IF_ADAPT_HOLD_SHIFT);
}

/* status (RO) */
uint32_t serdes_rx_if_get_pll_locked(void)
{
    return field_read(SERDES_RX_IF_PLL_LOCKED_OFFSET,
                      SERDES_RX_IF_PLL_LOCKED_MASK,
                      SERDES_RX_IF_PLL_LOCKED_SHIFT);
}

uint32_t serdes_rx_if_get_cdr_locked(void)
{
    return field_read(SERDES_RX_IF_CDR_LOCKED_OFFSET,
                      SERDES_RX_IF_CDR_LOCKED_MASK,
                      SERDES_RX_IF_CDR_LOCKED_SHIFT);
}

uint32_t serdes_rx_if_get_sig_detect(void)
{
    return field_read(SERDES_RX_IF_SIG_DETECT_OFFSET,
                      SERDES_RX_IF_SIG_DETECT_MASK,
                      SERDES_RX_IF_SIG_DETECT_SHIFT);
}

/* reset (RW) */
void serdes_rx_if_set_deser_rstn(uint32_t v)
{
    field_write(SERDES_RX_IF_DESER_RSTN_OFFSET, SERDES_RX_IF_DESER_RSTN_MASK,
                SERDES_RX_IF_DESER_RSTN_SHIFT, v);
}

uint32_t serdes_rx_if_get_deser_rstn(void)
{
    return field_read(SERDES_RX_IF_DESER_RSTN_OFFSET,
                      SERDES_RX_IF_DESER_RSTN_MASK,
                      SERDES_RX_IF_DESER_RSTN_SHIFT);
}

/* power (RW) */
void serdes_rx_if_set_en_afe_bias(uint32_t v)
{
    field_write(SERDES_RX_IF_EN_AFE_BIAS_OFFSET,
                SERDES_RX_IF_EN_AFE_BIAS_MASK,
                SERDES_RX_IF_EN_AFE_BIAS_SHIFT, v);
}

uint32_t serdes_rx_if_get_en_afe_bias(void)
{
    return field_read(SERDES_RX_IF_EN_AFE_BIAS_OFFSET,
                      SERDES_RX_IF_EN_AFE_BIAS_MASK,
                      SERDES_RX_IF_EN_AFE_BIAS_SHIFT);
}

void serdes_rx_if_set_en_pll(uint32_t v)
{
    field_write(SERDES_RX_IF_EN_PLL_OFFSET, SERDES_RX_IF_EN_PLL_MASK,
                SERDES_RX_IF_EN_PLL_SHIFT, v);
}

uint32_t serdes_rx_if_get_en_pll(void)
{
    return field_read(SERDES_RX_IF_EN_PLL_OFFSET, SERDES_RX_IF_EN_PLL_MASK,
                      SERDES_RX_IF_EN_PLL_SHIFT);
}

void serdes_rx_if_set_en_rx_fe(uint32_t v)
{
    field_write(SERDES_RX_IF_EN_RX_FE_OFFSET, SERDES_RX_IF_EN_RX_FE_MASK,
                SERDES_RX_IF_EN_RX_FE_SHIFT, v);
}

uint32_t serdes_rx_if_get_en_rx_fe(void)
{
    return field_read(SERDES_RX_IF_EN_RX_FE_OFFSET,
                      SERDES_RX_IF_EN_RX_FE_MASK,
                      SERDES_RX_IF_EN_RX_FE_SHIFT);
}

void serdes_rx_if_set_en_cdr(uint32_t v)
{
    field_write(SERDES_RX_IF_EN_CDR_OFFSET, SERDES_RX_IF_EN_CDR_MASK,
                SERDES_RX_IF_EN_CDR_SHIFT, v);
}

uint32_t serdes_rx_if_get_en_cdr(void)
{
    return field_read(SERDES_RX_IF_EN_CDR_OFFSET, SERDES_RX_IF_EN_CDR_MASK,
                      SERDES_RX_IF_EN_CDR_SHIFT);
}
