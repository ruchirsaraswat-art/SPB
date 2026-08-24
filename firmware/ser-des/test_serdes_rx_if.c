/*
 * test_serdes_rx_if.c
 * -------------------
 * Host unit tests for the serdes-rx-if register layer (plain C99, no
 * framework, no dynamic allocation). Builds with the stub register model
 * (reg_stub.c) providing the fw_reg_* port layer.
 *
 * main() returns 0 only if every check passed; one line printed per check.
 */

#include <stdio.h>
#include <stdint.h>

#include "serdes_rx_if_regs.h"
#include "serdes_rx_if_drv.h"
#include "reg_stub.h"

static int g_fail_count;

static void check(int cond, const char *name)
{
    printf("%s: %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) {
        g_fail_count++;
    }
}

/* ---- register-map metadata (mirrors serdes_rx_if_regs.h, table order) -- */

typedef struct {
    const char *name;   /* interface signal name */
    uint32_t offset;
    uint32_t mask;
    uint32_t shift;
    uint32_t width;
    int is_rw;          /* 1 = RW (d2a), 0 = RO (a2d) */
} map_entry_t;

static const map_entry_t k_map[SERDES_RX_IF_NUM_REGS] = {
    { "ctle_peak",      SERDES_RX_IF_CTLE_PEAK_OFFSET,      SERDES_RX_IF_CTLE_PEAK_MASK,      SERDES_RX_IF_CTLE_PEAK_SHIFT,      SERDES_RX_IF_CTLE_PEAK_WIDTH,      1 },
    { "vga_gain",       SERDES_RX_IF_VGA_GAIN_OFFSET,       SERDES_RX_IF_VGA_GAIN_MASK,       SERDES_RX_IF_VGA_GAIN_SHIFT,       SERDES_RX_IF_VGA_GAIN_WIDTH,       1 },
    { "dfe_tap1",       SERDES_RX_IF_DFE_TAP1_OFFSET,       SERDES_RX_IF_DFE_TAP1_MASK,       SERDES_RX_IF_DFE_TAP1_SHIFT,       SERDES_RX_IF_DFE_TAP1_WIDTH,       1 },
    { "slicer_ofst",    SERDES_RX_IF_SLICER_OFST_OFFSET,    SERDES_RX_IF_SLICER_OFST_MASK,    SERDES_RX_IF_SLICER_OFST_SHIFT,    SERDES_RX_IF_SLICER_OFST_WIDTH,    1 },
    { "eye_vref_code",  SERDES_RX_IF_EYE_VREF_CODE_OFFSET,  SERDES_RX_IF_EYE_VREF_CODE_MASK,  SERDES_RX_IF_EYE_VREF_CODE_SHIFT,  SERDES_RX_IF_EYE_VREF_CODE_WIDTH,  1 },
    { "eye_phase_code", SERDES_RX_IF_EYE_PHASE_CODE_OFFSET, SERDES_RX_IF_EYE_PHASE_CODE_MASK, SERDES_RX_IF_EYE_PHASE_CODE_SHIFT, SERDES_RX_IF_EYE_PHASE_CODE_WIDTH, 1 },
    { "adapt_hold",     SERDES_RX_IF_ADAPT_HOLD_OFFSET,     SERDES_RX_IF_ADAPT_HOLD_MASK,     SERDES_RX_IF_ADAPT_HOLD_SHIFT,     SERDES_RX_IF_ADAPT_HOLD_WIDTH,     1 },
    { "pll_locked",     SERDES_RX_IF_PLL_LOCKED_OFFSET,     SERDES_RX_IF_PLL_LOCKED_MASK,     SERDES_RX_IF_PLL_LOCKED_SHIFT,     SERDES_RX_IF_PLL_LOCKED_WIDTH,     0 },
    { "cdr_locked",     SERDES_RX_IF_CDR_LOCKED_OFFSET,     SERDES_RX_IF_CDR_LOCKED_MASK,     SERDES_RX_IF_CDR_LOCKED_SHIFT,     SERDES_RX_IF_CDR_LOCKED_WIDTH,     0 },
    { "sig_detect",     SERDES_RX_IF_SIG_DETECT_OFFSET,     SERDES_RX_IF_SIG_DETECT_MASK,     SERDES_RX_IF_SIG_DETECT_SHIFT,     SERDES_RX_IF_SIG_DETECT_WIDTH,     0 },
    { "deser_rstn",     SERDES_RX_IF_DESER_RSTN_OFFSET,     SERDES_RX_IF_DESER_RSTN_MASK,     SERDES_RX_IF_DESER_RSTN_SHIFT,     SERDES_RX_IF_DESER_RSTN_WIDTH,     1 },
    { "en_afe_bias",    SERDES_RX_IF_EN_AFE_BIAS_OFFSET,    SERDES_RX_IF_EN_AFE_BIAS_MASK,    SERDES_RX_IF_EN_AFE_BIAS_SHIFT,    SERDES_RX_IF_EN_AFE_BIAS_WIDTH,    1 },
    { "en_pll",         SERDES_RX_IF_EN_PLL_OFFSET,         SERDES_RX_IF_EN_PLL_MASK,         SERDES_RX_IF_EN_PLL_SHIFT,         SERDES_RX_IF_EN_PLL_WIDTH,         1 },
    { "en_rx_fe",       SERDES_RX_IF_EN_RX_FE_OFFSET,       SERDES_RX_IF_EN_RX_FE_MASK,       SERDES_RX_IF_EN_RX_FE_SHIFT,       SERDES_RX_IF_EN_RX_FE_WIDTH,       1 },
    { "en_cdr",         SERDES_RX_IF_EN_CDR_OFFSET,         SERDES_RX_IF_EN_CDR_MASK,         SERDES_RX_IF_EN_CDR_SHIFT,         SERDES_RX_IF_EN_CDR_WIDTH,         1 },
};

/* ---- log-scanning helpers --------------------------------------------- */

static uint32_t addr_of(uint32_t offset)
{
    return SERDES_RX_IF_BASE_ADDR + offset;
}

/* Index in the log of the n-th (0-based) WRITE access, or -1. */
static int nth_write(uint32_t n, uint32_t *addr_out, uint32_t *val_out)
{
    uint32_t i, seen = 0u;
    for (i = 0u; i < reg_stub_log_count(); i++) {
        const reg_stub_access_t *a = reg_stub_log_get(i);
        if (a->op == REG_STUB_OP_WRITE) {
            if (seen == n) {
                *addr_out = a->addr;
                *val_out = a->value;
                return (int)i;
            }
            seen++;
        }
    }
    return -1;
}

static uint32_t count_writes_of(uint32_t offset, uint32_t value)
{
    uint32_t i, n = 0u;
    for (i = 0u; i < reg_stub_log_count(); i++) {
        const reg_stub_access_t *a = reg_stub_log_get(i);
        if (a->op == REG_STUB_OP_WRITE && a->addr == addr_of(offset) &&
            a->value == value) {
            n++;
        }
    }
    return n;
}

/* First log index of a READ of `offset` at/after log index `from`, or -1. */
static int first_read_after(uint32_t offset, int from)
{
    uint32_t i;
    for (i = (uint32_t)from; i < reg_stub_log_count(); i++) {
        const reg_stub_access_t *a = reg_stub_log_get(i);
        if (a->op == REG_STUB_OP_READ && a->addr == addr_of(offset)) {
            return (int)i;
        }
    }
    return -1;
}

/* ---- test 1: offset uniqueness + 4-byte alignment ---------------------- */

static void test_offsets(void)
{
    uint32_t i, j;
    int unique = 1, aligned = 1, dense = 1;

    for (i = 0u; i < SERDES_RX_IF_NUM_REGS; i++) {
        if ((k_map[i].offset % 4u) != 0u) {
            aligned = 0;
        }
        /* Codegen rule: offset = 4 * index in group order. */
        if (k_map[i].offset != 4u * i) {
            dense = 0;
        }
        for (j = i + 1u; j < SERDES_RX_IF_NUM_REGS; j++) {
            if (k_map[i].offset == k_map[j].offset) {
                unique = 0;
            }
        }
    }
    check(unique, "map: all 15 register offsets unique");
    check(aligned, "map: all offsets 4-byte aligned");
    check(dense, "map: offset == 4*index per codegen rule (0x00..0x38)");
}

/* ---- test 2: field mask/shift round-trip per signal -------------------- */

static void test_field_roundtrip(void)
{
    uint32_t i;
    int masks_ok = 1, rw_roundtrip_ok = 1, rw_clamp_ok = 1;

    for (i = 0u; i < SERDES_RX_IF_NUM_REGS; i++) {
        uint32_t expect_mask =
            ((k_map[i].width >= 32u) ? 0xFFFFFFFFu
                                     : ((1u << k_map[i].width) - 1u))
            << k_map[i].shift;
        if (k_map[i].mask != expect_mask || k_map[i].shift != 0u) {
            masks_ok = 0;
        }
    }
    check(masks_ok, "map: MASK == ((1<<WIDTH)-1)<<SHIFT, SHIFT == 0, all fields");

    /* Round-trip max code through every RW register via the port layer. */
    reg_stub_reset();
    for (i = 0u; i < SERDES_RX_IF_NUM_REGS; i++) {
        uint32_t maxv;
        if (!k_map[i].is_rw) {
            continue;
        }
        maxv = k_map[i].mask >> k_map[i].shift;
        fw_reg_write32(addr_of(k_map[i].offset), maxv << k_map[i].shift);
        if (((fw_reg_read32(addr_of(k_map[i].offset)) & k_map[i].mask)
             >> k_map[i].shift) != maxv) {
            rw_roundtrip_ok = 0;
        }
    }
    check(rw_roundtrip_ok, "fields: max-code round-trip on every RW register");

    /* Driver setters must clamp to field width (spot-check each RW signal). */
    reg_stub_reset();
    serdes_rx_if_set_ctle_peak(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_ctle_peak() == 0xFu);      /* width 4 */
    serdes_rx_if_set_vga_gain(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_vga_gain() == 0x1Fu);      /* width 5 */
    serdes_rx_if_set_dfe_tap1(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_dfe_tap1() == 0x3Fu);      /* width 6 */
    serdes_rx_if_set_slicer_ofst(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_slicer_ofst() == 0x3Fu);   /* width 6 */
    serdes_rx_if_set_eye_vref_code(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_eye_vref_code() == 0x3Fu); /* width 6 */
    serdes_rx_if_set_eye_phase_code(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_eye_phase_code() == 0x3Fu);/* width 6 */
    serdes_rx_if_set_adapt_hold(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_adapt_hold() == 0x1u);     /* width 1 */
    serdes_rx_if_set_deser_rstn(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_deser_rstn() == 0x1u);
    serdes_rx_if_set_en_afe_bias(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_en_afe_bias() == 0x1u);
    serdes_rx_if_set_en_pll(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_en_pll() == 0x1u);
    serdes_rx_if_set_en_rx_fe(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_en_rx_fe() == 0x1u);
    serdes_rx_if_set_en_cdr(0xFFFFFFFFu);
    rw_clamp_ok &= (serdes_rx_if_get_en_cdr() == 0x1u);
    check(rw_clamp_ok, "fields: driver setters mask over-wide values to field width");
}

/* ---- test 3: RO writes ignored ---------------------------------------- */

static void test_ro_write_ignored(void)
{
    int ok = 1;
    uint32_t i;

    reg_stub_reset();
    for (i = 0u; i < SERDES_RX_IF_NUM_REGS; i++) {
        if (k_map[i].is_rw) {
            continue;
        }
        /* Write 1 via the driver's port layer; RO must stay 0. */
        fw_reg_write32(addr_of(k_map[i].offset), 1u);
        if (fw_reg_read32(addr_of(k_map[i].offset)) != 0u) {
            ok = 0;
        }
        /* Inject 1 (AFE side), then bus-write 0; RO must stay 1. */
        reg_stub_inject(k_map[i].offset, 1u);
        fw_reg_write32(addr_of(k_map[i].offset), 0u);
        if (fw_reg_read32(addr_of(k_map[i].offset)) != 1u) {
            ok = 0;
        }
        reg_stub_inject(k_map[i].offset, 0u);
    }
    check(ok, "RO: bus writes ignored on pll_locked/cdr_locked/sig_detect");

    /* Driver-level view: getter reflects injection, not the bus write. */
    reg_stub_reset();
    fw_reg_write32(addr_of(SERDES_RX_IF_PLL_LOCKED_OFFSET), 1u);
    ok = (serdes_rx_if_get_pll_locked() == 0u);
    reg_stub_inject(SERDES_RX_IF_PLL_LOCKED_OFFSET, 1u);
    ok &= (serdes_rx_if_get_pll_locked() == 1u);
    check(ok, "RO: driver getter sees injected status, not bus writes");
}

/* ---- test 4: power-up CSR write order matches power_sequence ----------- */

static void test_power_up_order(void)
{
    /* Expected write stream for power_sequence steps 1..6 with both lock
     * statuses pre-injected (locks found on first poll):
     *   step 1 safe state (reverse order), then steps 2,3,4,5,6. */
    static const struct { uint32_t offset; uint32_t value; } exp[] = {
        { SERDES_RX_IF_DESER_RSTN_OFFSET,  0u }, /* step 1 */
        { SERDES_RX_IF_EN_CDR_OFFSET,      0u }, /* step 1 */
        { SERDES_RX_IF_EN_RX_FE_OFFSET,    0u }, /* step 1 */
        { SERDES_RX_IF_EN_PLL_OFFSET,      0u }, /* step 1 */
        { SERDES_RX_IF_EN_AFE_BIAS_OFFSET, 0u }, /* step 1 */
        { SERDES_RX_IF_EN_AFE_BIAS_OFFSET, 1u }, /* step 2 */
        { SERDES_RX_IF_EN_PLL_OFFSET,      1u }, /* step 3 */
        { SERDES_RX_IF_EN_RX_FE_OFFSET,    1u }, /* step 4 */
        { SERDES_RX_IF_EN_CDR_OFFSET,      1u }, /* step 5 */
        { SERDES_RX_IF_DESER_RSTN_OFFSET,  1u }, /* step 6 */
    };
    const uint32_t nexp = (uint32_t)(sizeof(exp) / sizeof(exp[0]));
    uint32_t i, wa, wv, nwrites = 0u;
    int rc, order_ok = 1;
    int idx_en_pll = -1, idx_en_rx_fe = -1, idx_en_cdr = -1, idx_deser = -1;
    int idx_pll_rd, idx_cdr_rd;

    reg_stub_reset();
    reg_stub_inject(SERDES_RX_IF_PLL_LOCKED_OFFSET, 1u);
    reg_stub_inject(SERDES_RX_IF_CDR_LOCKED_OFFSET, 1u);

    rc = serdes_rx_if_power_up(NULL); /* NULL -> spec-seeded defaults */
    check(rc == SERDES_RX_IF_OK, "power_up: returns OK with locks injected");

    for (i = 0u; i < reg_stub_log_count(); i++) {
        if (reg_stub_log_get(i)->op == REG_STUB_OP_WRITE) {
            nwrites++;
        }
    }
    check(nwrites == nexp, "power_up: exact CSR write count (10)");

    for (i = 0u; i < nexp; i++) {
        int li = nth_write(i, &wa, &wv);
        if (li < 0 || wa != addr_of(exp[i].offset) || wv != exp[i].value) {
            order_ok = 0;
        }
        if (li >= 0 && wv == 1u) {
            if (wa == addr_of(SERDES_RX_IF_EN_PLL_OFFSET))   idx_en_pll = li;
            if (wa == addr_of(SERDES_RX_IF_EN_RX_FE_OFFSET)) idx_en_rx_fe = li;
            if (wa == addr_of(SERDES_RX_IF_EN_CDR_OFFSET))   idx_en_cdr = li;
            if (wa == addr_of(SERDES_RX_IF_DESER_RSTN_OFFSET)) idx_deser = li;
        }
    }
    check(order_ok, "power_up: write order matches power_sequence steps 1..6");

    /* wait_for gating: pll_locked polled between en_pll=1 and en_rx_fe=1;
     * cdr_locked polled between en_cdr=1 and deser_rstn=1. */
    idx_pll_rd = first_read_after(SERDES_RX_IF_PLL_LOCKED_OFFSET, idx_en_pll);
    idx_cdr_rd = first_read_after(SERDES_RX_IF_CDR_LOCKED_OFFSET, idx_en_cdr);
    check(idx_en_pll >= 0 && idx_pll_rd > idx_en_pll &&
          idx_pll_rd < idx_en_rx_fe,
          "power_up: pll_locked polled after en_pll=1, before step 4");
    check(idx_en_cdr >= 0 && idx_cdr_rd > idx_en_cdr && idx_cdr_rd < idx_deser,
          "power_up: cdr_locked polled after en_cdr=1, before deser release");

    check(reg_stub_bad_access_count() == 0u,
          "power_up: no accesses outside the register map");

    /* Settle waits actually consumed virtual time:
     * >= 20us (step 2) + 5us (step 4) + ceil(16 clk / 100 MHz)=1us (step 6). */
    check(reg_stub_elapsed_us() >= 26u,
          "power_up: settle delays taken from timing struct (>= 26 us)");

    /* Power-down: exact reverse order. */
    reg_stub_reset();
    rc = serdes_rx_if_power_down();
    order_ok = (rc == SERDES_RX_IF_OK) && (reg_stub_log_count() == 5u);
    {
        static const uint32_t down_exp[5] = {
            SERDES_RX_IF_DESER_RSTN_OFFSET, SERDES_RX_IF_EN_CDR_OFFSET,
            SERDES_RX_IF_EN_RX_FE_OFFSET,   SERDES_RX_IF_EN_PLL_OFFSET,
            SERDES_RX_IF_EN_AFE_BIAS_OFFSET
        };
        for (i = 0u; i < 5u; i++) {
            int li = nth_write(i, &wa, &wv);
            if (li < 0 || wa != addr_of(down_exp[i]) || wv != 0u) {
                order_ok = 0;
            }
        }
    }
    check(order_ok, "power_down: reverse order, en_afe_bias last off");
}

/* ---- test 5: timeout paths -------------------------------------------- */

static void test_timeouts(void)
{
    int rc;
    serdes_rx_if_timing_t t;

    /* pll_locked never injected -> step 3 timeout error, and the sequence
     * must stop before step 4 (no en_rx_fe=1 write). */
    reg_stub_reset();
    rc = serdes_rx_if_power_up(NULL);
    check(rc == SERDES_RX_IF_ERR_PLL_LOCK_TIMEOUT,
          "timeout: pll_locked never set -> ERR_PLL_LOCK_TIMEOUT");
    check(count_writes_of(SERDES_RX_IF_EN_RX_FE_OFFSET, 1u) == 0u,
          "timeout: sequence halts before step 4 on PLL timeout");
    check(reg_stub_elapsed_us() >=
              20u /* step-2 settle */ + 500u /* step-3 timeout */,
          "timeout: full pll_lock_timeout_us elapsed before error");

    /* cdr_locked never injected -> step 5 retries from step 4
     * (1 + cdr_retries = 4 attempts), then ERR_CDR_LOCK_TIMEOUT and no
     * deser_rstn release. */
    reg_stub_reset();
    reg_stub_inject(SERDES_RX_IF_PLL_LOCKED_OFFSET, 1u);
    serdes_rx_if_timing_default(&t);
    rc = serdes_rx_if_power_up(&t);
    check(rc == SERDES_RX_IF_ERR_CDR_LOCK_TIMEOUT,
          "timeout: cdr_locked never set -> ERR_CDR_LOCK_TIMEOUT");
    check(count_writes_of(SERDES_RX_IF_EN_CDR_OFFSET, 1u) == 1u + t.cdr_retries,
          "timeout: step 5 attempted 1 + cdr_retries (=4) times");
    check(count_writes_of(SERDES_RX_IF_DESER_RSTN_OFFSET, 1u) == 0u,
          "timeout: deser_rstn stays asserted on CDR failure");
}

/* ---- main -------------------------------------------------------------- */

int main(void)
{
    g_fail_count = 0;

    test_offsets();
    test_field_roundtrip();
    test_ro_write_ignored();
    test_power_up_order();
    test_timeouts();

    if (g_fail_count == 0) {
        printf("ALL CHECKS PASSED\n");
        return 0;
    }
    printf("%d CHECK(S) FAILED\n", g_fail_count);
    return 1;
}
