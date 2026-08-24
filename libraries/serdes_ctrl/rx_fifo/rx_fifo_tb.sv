`timescale 1ns/1fs
// ============================================================================
// rx_fifo_tb.sv -- self-checking testbench for rx_fifo (elastic FIFO).
// FIFO-class checks: fill_to_full, overflow_guard, drain_to_empty,
//                    pointer_wrap, ppm_stress, flags.
// Prints exactly one of RTL_TB_PASS / RTL_TB_FAIL <reason>, then $finish.
// Fixed random seed -> reproducible.
// ============================================================================
module rx_fifo_tb;

    // ---- catalog fields (mirror of the DUT defaults) ----
    localparam int DEPTH = 16;
    localparam int WIDTH = 16;
    localparam int PPM   = 300;
    localparam int K     = WIDTH / 8;
    localparam int SB    = 2;
    localparam logic [7:0] SKP = 8'h1C;
    localparam int AF_THRESH  = DEPTH - 4;
    localparam int AE_THRESH  = 4;
    localparam int AW = $clog2(DEPTH);
    localparam int PW = AW + 1;
    localparam int EW = WIDTH + K + SB;

    localparam logic [WIDTH-1:0] SKP_WORD = {K{SKP}};
    localparam logic [K-1:0]     SKP_K    = {K{1'b1}};

    // ---- test sizing ----
    localparam int WRAP_WORDS    = 8 * DEPTH;     // >= 4 x depth_words
    localparam int PPM_WORDS     = 20000;         // >= 10,000 words per direction
    localparam int SKP_INTERVAL  = 128;           // one SKP word every 128 words
    // Expected test length ~ 2*PPM_WORDS + a few thousand cycles (~45k rd_clk).
    // Watchdog >= 10x that.
    localparam int WATCHDOG_CYCLES = 600000;

    // ---- clocks ----
    localparam real RD_HALF = 5.0;               // 100 MHz clk_ref
    real            wr_half = 5.0;               // clk_rx_word, adjusted for ppm tests
    logic wr_clk = 0, rd_clk = 0;
    always #(wr_half) wr_clk = ~wr_clk;
    always #(RD_HALF) rd_clk = ~rd_clk;

    // ---- DUT signals ----
    logic             wr_rstn = 0, rd_rstn = 0;
    logic             wr_en = 0, rd_en = 0;
    logic [WIDTH-1:0] wr_data = '0;
    logic [K-1:0]     wr_k = '0;
    logic [SB-1:0]    wr_sb = '0;
    wire              full, almost_full, empty, almost_empty;
    wire  [PW-1:0]    wr_occupancy, rd_occupancy;
    wire              skp_del, skp_ins, rd_valid;
    wire  [WIDTH-1:0] rd_data;
    wire  [K-1:0]     rd_k;
    wire  [SB-1:0]    rd_sb;

    rx_fifo #(
        .DEPTH_WORDS(DEPTH), .WIDTH_BITS(WIDTH), .PPM_TOLERANCE(PPM),
        .K_LANES(K), .SB_WIDTH(SB), .SKP_SYMBOL(SKP),
        .ALMOST_FULL_THRESH(AF_THRESH), .ALMOST_EMPTY_THRESH(AE_THRESH)
    ) dut (
        .wr_clk(wr_clk), .wr_rstn(wr_rstn), .wr_en(wr_en),
        .wr_data(wr_data), .wr_k(wr_k), .wr_sb(wr_sb),
        .full(full), .almost_full(almost_full), .wr_occupancy(wr_occupancy),
        .skp_del(skp_del),
        .rd_clk(rd_clk), .rd_rstn(rd_rstn), .rd_en(rd_en),
        .rd_data(rd_data), .rd_k(rd_k), .rd_sb(rd_sb), .rd_valid(rd_valid),
        .empty(empty), .almost_empty(almost_empty), .rd_occupancy(rd_occupancy),
        .skp_ins(skp_ins)
    );

    // ---- bookkeeping ----
    int seed = 32'h5EED_0F1F;                       // FIXED seed
    logic [EW-1:0] refq[$];                         // reference queue
    int  errors = 0;
    int  mismatches = 0, rd_count = 0, skp_out_cnt = 0;
    int  total_mism = 0;                            // never reset: any escaped compare fails the TB
    int  ins_cnt = 0, del_cnt = 0, ovf_cnt = 0, udf_cnt = 0;
    bit  mon_en = 0, ppm_mode = 0, ovf_arm = 0, udf_arm = 0;
    bit  writer_done = 0;

    function automatic logic [WIDTH-1:0] rnd_data();
        rnd_data = $random(seed);
    endfunction

    task automatic fail(input string reason);
        $display("RTL_TB_FAIL %s", reason);
        $finish;
    endtask

    task automatic check(input bit cond, input string chk, input string msg);
        if (!cond) begin
            errors++;
            $display("  [%s] FAIL: %s (t=%0t)", chk, msg, $time);
        end
    endtask

    task automatic report(input string chk);
        $display("CHECK %s : %s", chk, (errors == 0) ? "PASS" : "FAIL");
        if (errors != 0) fail({chk});
    endtask

    // ---- watchdog (independent) ----
    initial begin
        repeat (WATCHDOG_CYCLES) @(posedge rd_clk);
        $display("RTL_TB_FAIL watchdog_timeout");
        $finish;
    end

    // ---- read monitor: compares every valid output word to the reference queue ----
    always @(negedge rd_clk) begin
        if (mon_en && rd_valid) begin
            logic [EW-1:0] exp;
            rd_count++;
            if (ppm_mode && (rd_k == SKP_K) && (rd_data == SKP_WORD)) begin
                skp_out_cnt++;
            end else if (refq.size() == 0) begin
                mismatches++; total_mism++;
                $display("  monitor: unexpected word %h with empty reference queue (t=%0t)", rd_data, $time);
            end else begin
                exp = refq.pop_front();
                if ({rd_data, rd_k, rd_sb} !== exp) begin
                    mismatches++; total_mism++;
                    if (mismatches < 10)
                        $display("  monitor: mismatch got %h/%b/%b exp %h/%b/%b (t=%0t)",
                                 rd_data, rd_k, rd_sb, exp[EW-1 -: WIDTH], exp[SB +: K], exp[SB-1:0], $time);
                end
            end
        end
        if (skp_ins) ins_cnt++;
        if (udf_arm && rd_en && empty) udf_cnt++;
    end
    always @(negedge wr_clk) begin
        if (skp_del) del_cnt++;
        if (ovf_arm && wr_en && full) ovf_cnt++;
    end

    // ---- helpers ----
    task automatic do_reset();
        wr_en = 0; rd_en = 0; wr_rstn = 0; rd_rstn = 0;
        refq.delete();
        repeat (4) @(posedge wr_clk);
        repeat (4) @(posedge rd_clk);
        @(negedge wr_clk); wr_rstn = 1;
        @(negedge rd_clk); rd_rstn = 1;
        repeat (4) @(posedge rd_clk);
        repeat (4) @(posedge wr_clk);
    endtask

    // Single write: drive on negedge, released on next negedge (flags settled).
    task automatic push_word(input logic [WIDTH-1:0] d, input logic [K-1:0] k,
                             input logic [SB-1:0] sb, input bit track = 1);
        @(negedge wr_clk);
        wr_en = 1; wr_data = d; wr_k = k; wr_sb = sb;
        if (track) refq.push_back({d, k, sb});
        @(negedge wr_clk);
        wr_en = 0;
    endtask

    // Single read: rd_en for one cycle; result visible at the second negedge.
    task automatic pop_word();
        @(negedge rd_clk);
        rd_en = 1;
        @(negedge rd_clk);
        rd_en = 0;
        #1;   // let the negedge monitor compare this word before the caller proceeds
    endtask

    task automatic settle();
        repeat (6) @(posedge wr_clk);
        repeat (6) @(posedge rd_clk);
    endtask

    // ========================================================================
    // Main sequence
    // ========================================================================
    initial begin
        int i;
        logic [PW-1:0] wp_snap, rp_snap;
        logic [WIDTH-1:0] d;

        $display("rx_fifo_tb: DEPTH=%0d WIDTH=%0d PPM=%0d SKP=K28.0(0x%02h) seed=%08h",
                 DEPTH, WIDTH, PPM, SKP, seed);

        // --------------------------------------------------------------
        // fill_to_full : full asserts on exactly the DEPTH-th write
        // --------------------------------------------------------------
        do_reset();
        mon_en = 1; mismatches = 0; rd_count = 0;
        check(empty == 1, "fill_to_full", "not empty after reset");
        check(full  == 0, "fill_to_full", "full after reset");
        for (i = 0; i < DEPTH; i++) begin
            check(full == 0, "fill_to_full", $sformatf("full asserted before write %0d", i+1));
            check(wr_occupancy == i, "fill_to_full", $sformatf("wr_occupancy %0d != %0d", wr_occupancy, i));
            d = rnd_data();
            push_word(d, '0, $random(seed));
        end
        check(full == 1, "fill_to_full", "full not asserted after DEPTH-th write");
        check(wr_occupancy == DEPTH, "fill_to_full", "wr_occupancy != DEPTH");
        report("fill_to_full");

        // --------------------------------------------------------------
        // overflow_guard : 8 write attempts while full do nothing
        // --------------------------------------------------------------
        wp_snap = dut.wr_ptr_bin;
        for (i = 0; i < 8; i++) begin
            push_word(rnd_data(), '0, '0, 0);   // not tracked: must be dropped
            check(full == 1, "overflow_guard", "full dropped during overflow attempt");
            check(dut.wr_ptr_bin == wp_snap, "overflow_guard", "write pointer moved while full");
        end
        settle();
        check(rd_occupancy == DEPTH, "overflow_guard", "rd_occupancy != DEPTH after fill");
        // read out all DEPTH words, monitor compares against refq
        for (i = 0; i < DEPTH; i++) pop_word();
        check(mismatches == 0, "overflow_guard", "data mismatch on readout");
        check(rd_count == DEPTH, "overflow_guard", $sformatf("read %0d words, expected %0d", rd_count, DEPTH));
        check(refq.size() == 0, "overflow_guard", "reference queue not drained");
        report("overflow_guard");

        // --------------------------------------------------------------
        // drain_to_empty : refill, then read all; empty exactly after last;
        //                  8 reads while empty do not move the pointer
        // --------------------------------------------------------------
        do_reset();
        mismatches = 0; rd_count = 0;
        for (i = 0; i < DEPTH; i++) push_word(rnd_data(), '0, $random(seed));
        settle();
        for (i = 0; i < DEPTH; i++) begin
            check(empty == 0, "drain_to_empty", $sformatf("empty asserted before read %0d", i+1));
            check(rd_occupancy == DEPTH - i, "drain_to_empty", "rd_occupancy wrong during drain");
            pop_word();
        end
        check(empty == 1, "drain_to_empty", "empty not asserted after last read");
        // rd_valid is registered: it still shows the last successful pop at
        // this negedge; it must drop on the next edge with rd_en=0.
        @(negedge rd_clk);
        check(rd_valid == 0, "drain_to_empty", "rd_valid stuck high after drain");
        rp_snap = dut.rd_ptr_bin;
        rd_count = 0;
        for (i = 0; i < 8; i++) begin
            pop_word();
            check(empty == 1, "drain_to_empty", "empty dropped during underflow attempt");
            check(rd_valid == 0, "drain_to_empty", "rd_valid asserted while empty");
            check(dut.rd_ptr_bin == rp_snap, "drain_to_empty", "read pointer moved while empty");
        end
        check(rd_count == 0, "drain_to_empty", "monitor saw words during underflow attempts");
        check(mismatches == 0, "drain_to_empty", "data mismatch during drain");
        report("drain_to_empty");

        // --------------------------------------------------------------
        // pointer_wrap : streaming push+pop, matched rate, >= 4 x DEPTH
        // --------------------------------------------------------------
        do_reset();
        mismatches = 0; rd_count = 0;
        fork
            begin : wrap_writer
                int n;
                n = 0;
                while (n < WRAP_WORDS) begin
                    @(negedge wr_clk);
                    if (!full) begin
                        wr_en = 1; wr_data = rnd_data(); wr_k = '0; wr_sb = $random(seed);
                        refq.push_back({wr_data, wr_k, wr_sb});
                        n++;
                    end else begin
                        wr_en = 0;
                    end
                end
                @(negedge wr_clk); wr_en = 0;
            end
            begin : wrap_reader
                @(negedge rd_clk); rd_en = 1;
            end
        join
        // drain remaining
        i = 0;
        while (refq.size() != 0 && i < 4 * DEPTH) begin @(negedge rd_clk); i++; end
        @(negedge rd_clk); rd_en = 0;
        check(refq.size() == 0, "pointer_wrap", "reference queue not drained");
        check(rd_count == WRAP_WORDS, "pointer_wrap", $sformatf("read %0d words, expected %0d", rd_count, WRAP_WORDS));
        check(mismatches == 0, "pointer_wrap", $sformatf("%0d mismatches", mismatches));
        check(empty == 1, "pointer_wrap", "not empty after stream");
        check(dut.wr_ptr_bin == dut.rd_ptr_bin, "pointer_wrap", "pointers not equal after stream");
        report("pointer_wrap");

        // --------------------------------------------------------------
        // flags : almost_full / almost_empty at threshold-1 / threshold / threshold+1
        // --------------------------------------------------------------
        do_reset();
        mismatches = 0; rd_count = 0;
        for (i = 0; i < AF_THRESH - 1; i++) push_word(rnd_data(), '0, '0);
        settle();
        check(wr_occupancy == AF_THRESH - 1, "flags", "occupancy != AF-1");
        check(almost_full == 0, "flags", "almost_full asserted at threshold-1");
        push_word(rnd_data(), '0, '0);
        check(wr_occupancy == AF_THRESH, "flags", "occupancy != AF");
        check(almost_full == 1, "flags", "almost_full not asserted at threshold");
        push_word(rnd_data(), '0, '0);
        check(wr_occupancy == AF_THRESH + 1, "flags", "occupancy != AF+1");
        check(almost_full == 1, "flags", "almost_full not asserted at threshold+1");
        settle();
        // now occupancy = AF_THRESH+1 (13 by default); walk down through AE_THRESH
        check(almost_empty == 0, "flags", "almost_empty asserted at high occupancy");
        while (rd_occupancy > AE_THRESH + 1) pop_word();
        settle();
        check(rd_occupancy == AE_THRESH + 1, "flags", "occupancy != AE+1");
        check(almost_empty == 0, "flags", "almost_empty asserted at threshold+1");
        pop_word();
        check(rd_occupancy == AE_THRESH, "flags", "occupancy != AE");
        check(almost_empty == 1, "flags", "almost_empty not asserted at threshold");
        pop_word();
        check(rd_occupancy == AE_THRESH - 1, "flags", "occupancy != AE-1");
        check(almost_empty == 1, "flags", "almost_empty not asserted at threshold-1");
        settle();
        check(almost_full == 0, "flags", "almost_full still asserted at low occupancy");
        while (!empty) pop_word();
        check(mismatches == 0, "flags", "data mismatch during flags test");
        report("flags");

        // --------------------------------------------------------------
        // ppm_stress : writer +300 ppm (faster) then -300 ppm (slower)
        // --------------------------------------------------------------
        ppm_run(RD_HALF * (1.0 - PPM / 1.0e6), 1'b1, 1'b0, "ppm_stress(writer_fast)");
        ppm_run(RD_HALF * (1.0 + PPM / 1.0e6), 1'b0, 1'b1, "ppm_stress(writer_slow)");
        wr_half = RD_HALF;
        report("ppm_stress");

        // global guard: no output word anywhere in the run escaped comparison
        check(total_mism == 0, "global", $sformatf("%0d total monitor mismatches across all tests", total_mism));
        report("global");

        $display("RTL_TB_PASS");
        $finish;
    end

    // One plesiochronous run: continuous writer with periodic SKP words,
    // reader starts once the FIFO is half full and then reads every cycle.
    task automatic ppm_run(input real wr_half_ns, input bit expect_del, input bit expect_ins,
                           input string tag);
        int n_skp_written;
        int i;
        do_reset();
        wr_half = wr_half_ns;
        repeat (4) @(posedge wr_clk);
        ppm_mode = 1; mon_en = 1;
        mismatches = 0; rd_count = 0; skp_out_cnt = 0;
        ins_cnt = 0; del_cnt = 0; ovf_cnt = 0; udf_cnt = 0;
        n_skp_written = 0; writer_done = 0;
        ovf_arm = 1;
        fork
            begin : ppm_writer
                for (i = 0; i < PPM_WORDS; i++) begin
                    @(negedge wr_clk);
                    wr_en = 1;
                    if ((i % SKP_INTERVAL) == (SKP_INTERVAL - 1)) begin
                        wr_data = SKP_WORD; wr_k = SKP_K; wr_sb = '0;
                        n_skp_written++;
                    end else begin
                        wr_data = rnd_data(); wr_k = '0; wr_sb = $random(seed);
                        refq.push_back({wr_data, wr_k, wr_sb});
                    end
                end
                @(negedge wr_clk); wr_en = 0;
                writer_done = 1;
            end
            begin : ppm_reader
                wait (rd_occupancy >= DEPTH / 2);
                @(negedge rd_clk); rd_en = 1; udf_arm = 1;
                wait (writer_done);
                udf_arm = 0;
            end
        join
        ovf_arm = 0;
        // drain
        i = 0;
        while (refq.size() != 0 && i < 4 * DEPTH) begin @(negedge rd_clk); i++; end
        @(negedge rd_clk); rd_en = 0;
        repeat (4) @(negedge rd_clk);
        $display("  %s: wr_half=%.4f ns words=%0d skp_written=%0d skp_out=%0d ins=%0d del=%0d ovf=%0d udf=%0d mism=%0d",
                 tag, wr_half_ns, PPM_WORDS, n_skp_written, skp_out_cnt, ins_cnt, del_cnt, ovf_cnt, udf_cnt, mismatches);
        check(ovf_cnt == 0, tag, "overflow (write attempted while full)");
        check(udf_cnt == 0, tag, "underflow (read attempted while empty)");
        check(mismatches == 0, tag, "data mismatch (skips excluded)");
        check(refq.size() == 0, tag, "reference queue not drained");
        check(rd_count == PPM_WORDS - n_skp_written + skp_out_cnt, tag, "word count inconsistent");
        check(skp_out_cnt == n_skp_written + ins_cnt - del_cnt, tag, "SKP conservation violated");
        if (expect_del) check(del_cnt > 0, tag, "no SKP delete events with faster writer");
        if (expect_ins) check(ins_cnt > 0, tag, "no SKP insert events with slower writer");
        ppm_mode = 0;
    endtask

endmodule
