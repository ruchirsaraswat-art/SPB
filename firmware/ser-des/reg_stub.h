/*
 * reg_stub.h
 * ----------
 * Host stub register model for interface "serdes-rx-if" (host tests only,
 * plain gcc). Defines the fw_reg_read32/fw_reg_write32/fw_delay_us port
 * layer against an array-backed register file covering the serdes_rx_if
 * map, with:
 *   - RO (a2d status) registers: bus writes ignored;
 *   - status-injection hooks so a test can assert pll_locked etc.;
 *   - an access-order log for sequence-order tests;
 *   - a virtual-time counter advanced by fw_delay_us.
 */

#ifndef REG_STUB_H
#define REG_STUB_H

#include <stdint.h>
#include "serdes_rx_if_regs.h"

/* One logged bus access, in the order the driver issued it. */
typedef enum {
    REG_STUB_OP_READ  = 0,
    REG_STUB_OP_WRITE = 1
} reg_stub_op_t;

typedef struct {
    reg_stub_op_t op;
    uint32_t      addr;   /* full bus address                           */
    uint32_t      value;  /* value written, or value returned by a read */
} reg_stub_access_t;

#define REG_STUB_LOG_CAPACITY 4096u

/* Reset the model: all registers 0 (interface reset step 1: enables
 * cleared, deser_rstn=0, trim codes to defaults), log emptied, virtual
 * time zeroed. Call at the start of every test. */
void reg_stub_reset(void);

/* Status-injection hook: force the raw value of any register (including RO
 * status regs pll_locked/cdr_locked/sig_detect) by map offset. This models
 * the AFE driving an a2d status input; it bypasses the RO write protection
 * and is NOT logged as a bus access. */
void reg_stub_inject(uint32_t offset, uint32_t value);

/* Peek the raw register value by map offset (not logged). */
uint32_t reg_stub_peek(uint32_t offset);

/* Access log. */
uint32_t reg_stub_log_count(void);
const reg_stub_access_t *reg_stub_log_get(uint32_t index); /* NULL if OOR */

/* Virtual time accumulated by fw_delay_us since reg_stub_reset(). */
uint32_t reg_stub_elapsed_us(void);

/* Count of accesses that hit addresses outside the serdes_rx_if map
 * (should stay 0 in every test). */
uint32_t reg_stub_bad_access_count(void);

#endif /* REG_STUB_H */
