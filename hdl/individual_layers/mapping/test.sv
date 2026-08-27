// SPDX-License-Identifier: MIT
// Copyright (c) 2025 Gwangsun Shin

`timescale 1ns/1ps
`include "axi4s_io.sv"
`include "Generator.sv"
`include "Driver.sv"
`include "Receiver.sv"
`include "Scoreboard.sv"
`include "pixelTypes.h"


module tb_mappingLayer;
  logic clk = 0;
  axi4s_io tb_if(clk);
  
AxiMappingLayer DUT(
   .i_clk(clk),
   .i_rstn(tb_if.i_rstn),
   .s_axis_tready(tb_if.s_axis_tready),
   .s_axis_tvalid(tb_if.s_axis_tvalid),
   .s_axis_tdata(tb_if.s_axis_tdata), /* MSB: Ch12 ~ LSB:CH0 */
   .m_axis_tdata(tb_if.m_axis_tdata),
   .m_axis_tvalid(tb_if.m_axis_tvalid),
   .m_axis_tready(tb_if.m_axis_tready),
   .EOL(tb_if.eol),
   .EOF(tb_if.eof)
 );

//Axi4ConvCore DUT(
//    .EOF_0(tb_if.eof),
//    .EOL_0(tb_if.eol),
//    .i_clk_0(clk),
//    .i_rstn_0(tb_if.i_rstn),
//    .m_axis_0_tdata(tb_if.m_axis_tdata),
//    .m_axis_0_tready(tb_if.m_axis_tready),
//    .m_axis_0_tvalid(tb_if.m_axis_tvalid),
//    .s_axis_0_tdata(tb_if.s_axis_tdata),
//    .s_axis_0_tready(tb_if.s_axis_tready),
//    .s_axis_0_tvalid(tb_if.s_axis_tvalid)
//    );

   mailbox #(in_pixel_t) mbox_drv = new();
   mailbox #(out_pixel_t) mbox_Recv = new();
   Generator gen;
   Driver drv;
   Receiver recv;
   Scoreboard sc;

  always #5 clk = ~clk;


  initial begin
   automatic int width = 320;
   automatic int depth = 180;
   automatic int out_width = 320;
   automatic int out_depth = 180;
   automatic int frame_count = 2;
   automatic int burst_len = 600;
   automatic int pause_cycles = 5;
   automatic int master_ready_burst_len = 5000;
   automatic int master_ready_pause_cycles = 10000;
//   $dumpfile("sim.vcd");
//   $dumpvars(0, tb_mappingLayer);
   gen = new("gen", width, depth, frame_count, mbox_drv);
   drv = new(tb_if.TB, mbox_drv, burst_len, pause_cycles, width * depth * frame_count);
   recv = new(tb_if.TB, mbox_Recv , master_ready_burst_len , master_ready_pause_cycles);
   sc = new(mbox_Recv, out_width, out_depth, frame_count);

   fork
      drv.run();
      gen.run();
      recv.run();
      sc.run();
   join
  end
endmodule