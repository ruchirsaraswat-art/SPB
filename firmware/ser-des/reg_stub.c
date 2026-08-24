/*
 * reg_stub.c
 * ----------
 * Host stub register model - see reg_stub.h. Static storage only (no
 * dynamic allocation), C99.
 */

#include "reg_stub.h"

#include <stddef.h> /* NULL */

/* ---- register file ---------------------------------------------------- */

static uint32_t s_regs[SERDES_RX_IF_NUM_REGS];

/* RO (a2d status) offsets - bus writes ignored, matching the codegen rule
 * "a2d = RO (stub ignores writes)". */
static int offset_is_ro(uint32_t offset)
{
    return (offset == SERDES_RX_IF_PLL_LOCKED_OFFSET) ||
           (offset == SERDES_RX_IF_CDR_LOCKED_OFFSET) ||
           (offset == SERDES_RX_IF_SIG_DETECT_OFFSET);
}

/* Map a bus address to a register index; -1 if outside the map. */
static int addr_to_index(uint32_t addr)
{
    uint32_t offset;

    if (addr < SERDES_RX_IF_BASE_ADDR) {
        return -1;
    }
    offset = addr - SERDES_RX_IF_BASE_ADDR;
    if ((offset % 4u) != 0u || (offset / 4u) >= SERDES_RX_IF_NUM_REGS) {
        return -1;
    }
    return (int)(offset / 4u);
}

/* ---- access log and counters ------------------------------------------ */

static reg_stub_access_t s_log[REG_STUB_LOG_CAPACITY];
static uint32_t s_log_count;
static uint32_t s_bad_access_count;
static uint32_t s_elapsed_us;

static void log_access(reg_stub_op_t op, uint32_t addr, uint32_t value)
{
    if (s_log_count < REG_STUB_LOG_CAPACITY) {
        s_log[s_log_count].op = op;
        s_log[s_log_count].addr = addr;
        s_log[s_log_count].value = value;
        s_log_count++;
    }
    /* On overflow the log saturates; tests keep well below capacity. */
}

/* ---- public stub control ---------------------------------------------- */

void reg_stub_reset(void)
{
    uint32_t i;
    for (i = 0u; i < SERDES_RX_IF_NUM_REGS; i++) {
        s_regs[i] = 0u;
    }
    s_log_count = 0u;
    s_bad_access_count = 0u;
    s_elapsed_us = 0u;
}

void reg_stub_inject(uint32_t offset, uint32_t value)
{
    int idx = addr_to_index(SERDES_RX_IF_BASE_ADDR + offset);
    if (idx >= 0) {
        s_regs[idx] = value; /* bypasses RO protection; not logged */
    }
}

uint32_t reg_stub_peek(uint32_t offset)
{
    int idx = addr_to_index(SERDES_RX_IF_BASE_ADDR + offset);
    return (idx >= 0) ? s_regs[idx] : 0u;
}

uint32_t reg_stub_log_count(void)
{
    return s_log_count;
}

const reg_stub_access_t *reg_stub_log_get(uint32_t index)
{
    return (index < s_log_count) ? &s_log[index] : NULL;
}

uint32_t reg_stub_elapsed_us(void)
{
    return s_elapsed_us;
}

uint32_t reg_stub_bad_access_count(void)
{
    return s_bad_access_count;
}

/* ---- port layer definitions (contract in serdes_rx_if_drv.h) ---------- */

uint32_t fw_reg_read32(uint32_t addr)
{
    int idx = addr_to_index(addr);
    uint32_t v = 0u;

    if (idx >= 0) {
        v = s_regs[idx];
    } else {
        s_bad_access_count++;
    }
    log_access(REG_STUB_OP_READ, addr, v);
    return v;
}

void fw_reg_write32(uint32_t addr, uint32_t v)
{
    int idx = addr_to_index(addr);

    log_access(REG_STUB_OP_WRITE, addr, v);
    if (idx < 0) {
        s_bad_access_count++;
        return;
    }
    if (offset_is_ro(addr - SERDES_RX_IF_BASE_ADDR)) {
        return; /* RO: write ignored */
    }
    s_regs[idx] = v;
}

void fw_delay_us(uint32_t us)
{
    /* Host model: advance virtual time only. On a real target this would be
     * a timer-calibrated busy-wait or OS sleep (needs the target toolchain,
     * intentionally not assumed here). */
    s_elapsed_us += us;
}
