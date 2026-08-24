# Makefile - serdes-rx-if register-layer host tests
# Host gcc only (per v1 scope). A real target build would need the target
# cross-toolchain plus a target port file defining fw_reg_read32 /
# fw_reg_write32 / fw_delay_us in place of reg_stub.c.

CC     = gcc
CFLAGS = -std=c99 -Wall -Werror

SRCS = serdes_rx_if_drv.c reg_stub.c test_serdes_rx_if.c
HDRS = serdes_rx_if_regs.h serdes_rx_if_drv.h reg_stub.h
BIN  = test_serdes_rx_if

.PHONY: all test clean

all: $(BIN)

$(BIN): $(SRCS) $(HDRS)
	$(CC) $(CFLAGS) -o $(BIN) $(SRCS)

test: $(BIN)
	./$(BIN)

clean:
	rm -f $(BIN)
