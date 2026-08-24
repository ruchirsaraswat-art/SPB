`timescale 1ns/1ps
// ============================================================================
// rx_fifo.v -- Elastic FIFO (CDC clk_rx_word -> clk_ref) for the ser-des PHY
//              RX datapath.  Block id: rx-fifo, topology: elastic_fifo.
//
// Position in the digital architecture:  rx-dec -> rx-fifo -> rx-pcs
//   Write side : clk_rx_word (CDR-recovered word clock, owner AFE/deserializer)
//   Read side  : clk_ref     (100 MHz core / CSR / sequencer domain)
//   The two clocks are plesiochronous, +/- PPM_TOLERANCE ppm (interface
//   definition clock_domains.clk_rx_word).  Phase is absorbed by the
//   gray-pointer async FIFO; frequency offset is absorbed by SKP symbol
//   deletion (write side) and SKP symbol insertion (read side).
//
// Catalog fields -> parameters (never hard-coded):
//   depth_words   = 16  -> DEPTH_WORDS  (must be a power of two: gray pointers)
//   width_bits    = 16  -> WIDTH_BITS
//   ppm_tolerance = 300 -> PPM_TOLERANCE (documentation / sizing localparam)
//   skip_symbol   = NOT SET in catalog.  ASSUMED DEFAULT: K28.0 (8b/10b
//                   control code 8'h1C with K flag), the conventional SKP
//                   symbol.  A SKP *word* is every 8-bit lane of the word
//                   equal to SKP_SYMBOL with every lane K flag set.
//
// Interface-definition context used:
//   cdc_points rx_pdata / rx_err_sample / rx_err_valid : strategy async_fifo,
//     from clk_rx_word to clk_ref, "this IS the elastic FIFO block".
//     rx_err_sample/rx_err_valid "travel with rx_pdata" -> generic sideband
//     port wr_sb/rd_sb (SB_WIDTH, default 2) carried through the same entry.
//   reset_sequence step 2: POR release synchronized per domain -> this block
//     takes two per-domain, already-synchronized, active-low SYNCHRONOUS
//     resets (wr_rstn in clk_rx_word, rd_rstn in clk_ref).  Both must be
//     asserted together (pwr-seq owns that); pointer coherency is not
//     guaranteed if only one domain is reset.
//
// Rate-compensation policy (agent judgment, documented):
//   * Write side deletes an incoming SKP word when its (conservative, >=
//     actual) occupancy view is > SKP_DEL_THRESH  (default DEPTH/2 + 2).
//     A write attempt while full is always ignored (overflow guard); a SKP
//     dropped for that reason is also reported on skp_del.
//   * Read side inserts (repeats the head SKP without popping) when its
//     (conservative, <= actual) occupancy view is < SKP_INS_THRESH
//     (default DEPTH/2 - 2) and the head word is a SKP.  This also performs
//     initial centering: the first SKP after reset is held until the FIFO
//     is refilled to the threshold.  A read attempt while empty is ignored
//     (rd_valid = 0, underflow guard).
//   * Sizing bound (see MAX_SKP_INTERVAL_WORDS): with headroom H words
//     between the centre and a threshold, and worst-case relative offset
//     2*PPM_TOLERANCE, SKP words must arrive at least every
//     H * 1e6 / (2 * PPM_TOLERANCE) words.  Default: 6e6/600 = 10,000 words.
//   * Compensation is unconditional (no enable input), so no extra CSR
//     control crossing is introduced into the clk_rx_word domain.
//
// CDC summary (all commented inline with "// CDC:"):
//   wr_ptr_gray : clk_rx_word -> clk_ref      2-flop sync of gray pointer
//   rd_ptr_gray : clk_ref     -> clk_rx_word  2-flop sync of gray pointer
//   mem[]       : written in clk_rx_word, read in clk_ref; data is stable
//                 by the time the synchronized pointer exposes the entry
//                 (standard async-FIFO argument).
//
// Synthesizable Verilog-2005 subset: posedge-only always blocks, synchronous
// resets, no delays, no initial blocks, no latches, no $display.
// ============================================================================
module rx_fifo #(
    parameter         DEPTH_WORDS         = 16,               // catalog depth_words
    parameter         WIDTH_BITS          = 16,               // catalog width_bits
    parameter         PPM_TOLERANCE       = 300,              // catalog ppm_tolerance
    parameter         K_LANES             = WIDTH_BITS / 8,   // one K flag per 8b lane
    parameter         SB_WIDTH            = 2,                // sideband (rx_err_sample, rx_err_valid)
    parameter [7:0]   SKP_SYMBOL          = 8'h1C,            // ASSUMED: K28.0 (catalog skip_symbol not set)
    parameter         ALMOST_FULL_THRESH  = DEPTH_WORDS - 4,  // almost_full  when wr occupancy >= this
    parameter         ALMOST_EMPTY_THRESH = 4,                // almost_empty when rd occupancy <= this
    parameter         SKP_DEL_THRESH      = DEPTH_WORDS / 2 + 2, // delete SKP when wr occupancy >  this
    parameter         SKP_INS_THRESH      = DEPTH_WORDS / 2 - 2  // insert SKP when rd occupancy <  this
) (
    // ---------------- write side : clk_rx_word domain ----------------
    input  wire                   wr_clk,        // clk_rx_word
    input  wire                   wr_rstn,       // sync active-low, released in clk_rx_word
    input  wire                   wr_en,
    input  wire [WIDTH_BITS-1:0]  wr_data,       // decoded data word
    input  wire [K_LANES-1:0]     wr_k,          // per-lane K (control symbol) flags
    input  wire [SB_WIDTH-1:0]    wr_sb,         // sideband travelling with the word
    output wire                   full,
    output wire                   almost_full,
    output wire [$clog2(DEPTH_WORDS):0] wr_occupancy, // write-side view (>= actual)
    output reg                    skp_del,       // 1-cycle pulse: a SKP word was deleted
    // ---------------- read side : clk_ref domain ----------------
    input  wire                   rd_clk,        // clk_ref
    input  wire                   rd_rstn,       // sync active-low, released in clk_ref
    input  wire                   rd_en,
    output reg  [WIDTH_BITS-1:0]  rd_data,
    output reg  [K_LANES-1:0]     rd_k,
    output reg  [SB_WIDTH-1:0]    rd_sb,
    output reg                    rd_valid,      // rd_data/rd_k/rd_sb valid this cycle
    output wire                   empty,
    output wire                   almost_empty,
    output wire [$clog2(DEPTH_WORDS):0] rd_occupancy, // read-side view (<= actual)
    output reg                    skp_ins        // 1-cycle pulse: a SKP word was inserted
);

    // ------------------------------------------------------------------
    // Derived constants
    // ------------------------------------------------------------------
    localparam AW = $clog2(DEPTH_WORDS);          // address width
    localparam PW = AW + 1;                       // pointer width (extra wrap bit)
    localparam EW = WIDTH_BITS + K_LANES + SB_WIDTH; // memory entry width
    // Documentation-only sizing bound derived from ppm_tolerance (see header).
    localparam SKP_HEADROOM_WORDS     = DEPTH_WORDS / 2 - 2;
    localparam MAX_SKP_INTERVAL_WORDS = (SKP_HEADROOM_WORDS * 1000000) / (2 * PPM_TOLERANCE);

    localparam [WIDTH_BITS-1:0] SKP_WORD = {K_LANES{SKP_SYMBOL}};
    localparam [K_LANES-1:0]    SKP_K    = {K_LANES{1'b1}};

    // ------------------------------------------------------------------
    // Gray <-> binary helpers
    // ------------------------------------------------------------------
    function [PW-1:0] bin2gray;
        input [PW-1:0] b;
        begin
            bin2gray = b ^ (b >> 1);
        end
    endfunction

    function [PW-1:0] gray2bin;
        input [PW-1:0] g;
        integer i;
        begin
            gray2bin[PW-1] = g[PW-1];
            for (i = PW-2; i >= 0; i = i - 1)
                gray2bin[i] = gray2bin[i+1] ^ g[i];
        end
    endfunction

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------
    reg [EW-1:0] mem [0:DEPTH_WORDS-1];

    // ------------------------------------------------------------------
    // Pointers
    // ------------------------------------------------------------------
    reg  [PW-1:0] wr_ptr_bin, wr_ptr_gray;   // clk_rx_word domain
    reg  [PW-1:0] rd_ptr_bin, rd_ptr_gray;   // clk_ref domain

    // CDC: rd_ptr_gray  clk_ref -> clk_rx_word  (2-flop synchronizer, gray
    //      coded so at most one bit changes per increment).  Part of the
    //      async_fifo strategy for rx_pdata/rx_err_* in cdc_points.
    reg  [PW-1:0] rd_gray_s1, rd_gray_s2;
    // CDC: wr_ptr_gray  clk_rx_word -> clk_ref  (2-flop synchronizer, gray).
    reg  [PW-1:0] wr_gray_s1, wr_gray_s2;

    // ------------------------------------------------------------------
    // Write side (clk_rx_word)
    // ------------------------------------------------------------------
    wire [PW-1:0] rd_ptr_bin_wsync = gray2bin(rd_gray_s2);
    wire [PW-1:0] wr_occ           = wr_ptr_bin - rd_ptr_bin_wsync; // >= actual

    assign wr_occupancy = wr_occ;
    assign full         = (wr_occ == DEPTH_WORDS[PW-1:0]);
    assign almost_full  = (wr_occ >= ALMOST_FULL_THRESH[PW-1:0]);

    wire wr_is_skp = (wr_k == SKP_K) && (wr_data == SKP_WORD);
    // Delete: SKP arriving while occupancy view is above the delete threshold
    // (this includes the full case: DEPTH_WORDS > SKP_DEL_THRESH).
    wire wr_del    = wr_en & wr_is_skp & (wr_occ > SKP_DEL_THRESH[PW-1:0]);
    wire wr_push   = wr_en & ~full & ~wr_del;

    always @(posedge wr_clk) begin
        if (!wr_rstn) begin
            wr_ptr_bin  <= {PW{1'b0}};
            wr_ptr_gray <= {PW{1'b0}};
            rd_gray_s1  <= {PW{1'b0}};
            rd_gray_s2  <= {PW{1'b0}};
            skp_del     <= 1'b0;
        end else begin
            // CDC: 2-flop synchronizer clk_ref -> clk_rx_word (rd_ptr_gray)
            rd_gray_s1  <= rd_ptr_gray;
            rd_gray_s2  <= rd_gray_s1;
            skp_del     <= wr_del;
            if (wr_push) begin
                wr_ptr_bin  <= wr_ptr_bin + 1'b1;
                wr_ptr_gray <= bin2gray(wr_ptr_bin + 1'b1);
            end
        end
    end

    // Memory write (no reset on the array)
    always @(posedge wr_clk) begin
        if (wr_push)
            mem[wr_ptr_bin[AW-1:0]] <= {wr_data, wr_k, wr_sb};
    end

    // ------------------------------------------------------------------
    // Read side (clk_ref)
    // ------------------------------------------------------------------
    wire [PW-1:0] wr_ptr_bin_rsync = gray2bin(wr_gray_s2);
    wire [PW-1:0] rd_occ           = wr_ptr_bin_rsync - rd_ptr_bin; // <= actual

    assign rd_occupancy = rd_occ;
    assign empty        = (rd_occ == {PW{1'b0}});
    assign almost_empty = (rd_occ <= ALMOST_EMPTY_THRESH[PW-1:0]);

    wire [EW-1:0]         head      = mem[rd_ptr_bin[AW-1:0]];
    wire [WIDTH_BITS-1:0] head_data = head[EW-1 -: WIDTH_BITS];
    wire [K_LANES-1:0]    head_k    = head[SB_WIDTH +: K_LANES];
    wire [SB_WIDTH-1:0]   head_sb   = head[SB_WIDTH-1:0];
    wire head_is_skp = (head_k == SKP_K) && (head_data == SKP_WORD);

    wire rd_take = rd_en & ~empty;
    // Insert: present the head SKP again without popping while the read
    // side's occupancy view is below the insert threshold.
    wire rd_ins  = rd_take & head_is_skp & (rd_occ < SKP_INS_THRESH[PW-1:0]);
    wire rd_pop  = rd_take & ~rd_ins;

    always @(posedge rd_clk) begin
        if (!rd_rstn) begin
            rd_ptr_bin  <= {PW{1'b0}};
            rd_ptr_gray <= {PW{1'b0}};
            wr_gray_s1  <= {PW{1'b0}};
            wr_gray_s2  <= {PW{1'b0}};
            rd_valid    <= 1'b0;
            rd_data     <= {WIDTH_BITS{1'b0}};
            rd_k        <= {K_LANES{1'b0}};
            rd_sb       <= {SB_WIDTH{1'b0}};
            skp_ins     <= 1'b0;
        end else begin
            // CDC: 2-flop synchronizer clk_rx_word -> clk_ref (wr_ptr_gray)
            wr_gray_s1  <= wr_ptr_gray;
            wr_gray_s2  <= wr_gray_s1;
            rd_valid    <= rd_take;
            skp_ins     <= rd_ins;
            if (rd_take) begin
                rd_data <= head_data;
                rd_k    <= head_k;
                rd_sb   <= head_sb;
            end
            if (rd_pop) begin
                rd_ptr_bin  <= rd_ptr_bin + 1'b1;
                rd_ptr_gray <= bin2gray(rd_ptr_bin + 1'b1);
            end
        end
    end

endmodule
