// SPDX-License-Identifier: MIT
// Copyright (c) 2025 Gwangsun Shin

`ifndef IMAGE_PROCESS_PACKAGE
`define IMAGE_PROCESS_PACKAGE
package imageProcessPkg;
`include "pixelTypes.h"

//////////////////////////////////////////////////
////////// evluation function ///////////////////
//////////////////////////////////////////////////
function automatic real abs_real(input real x);
  return (x < 0) ? -x : x;
endfunction : abs_real

// Returns 0 if the relative error between value1 and value2 is below the threshold, otherwise returns 1.
function automatic int valueCompare(
  input real value1,
  input real value2,
  input real threshold,
  input real maxValue
);
 real difference;
 difference = abs_real((value1 - value2) / maxValue);
//  $display("Percentage error: %0f%%", difference * 100);
 if (difference < threshold)
   return 0;
 else
   return 1;
endfunction : valueCompare

function automatic int totalPixelEval(
  input int x,
  input int y,
  input int frame,
  input out_pixel_t refPixelPacked,
  input out_pixel_t dutPixelPacked,
  input real       threshold,
  input real       maxValue
);
  int refPixel   [OUT_NPIX];
  int dutPixel   [OUT_NPIX];
  int anyFail = 0;

  for (int i = 0; i < OUT_NPIX; i++) begin
    refPixel[i] = refPixelPacked[8*i +:8];
    dutPixel[i] = dutPixelPacked[8*i +:8];
    if (!valueCompare(real'(refPixel[i]),
                      real'(dutPixel[i]),
                      threshold,
                      maxValue) ) begin
      $display("[%0t] [Frame:%0d][x:%0d][y:%0d]   Channel %0d SUCCESS: ref=%0h, dut=%0h",
               $realtime,frame, x, y, i+1, refPixel[i], dutPixel[i]);
    end
    else begin
      $display("[%0t] [Frame:%0d][x:%0d][y:%0d]   Channel %0d FAIL: ref=%0h, dut=%0h",
               $realtime,frame, x, y, i+1, refPixel[i], dutPixel[i]);
      anyFail = 1;
    end
  end

  return anyFail;
endfunction



//////////////////////////////////////////////////
////////// reference Txt Load function ///////////
//////////////////////////////////////////////////

function automatic void readPixelTxt_RGB(
  string          filename,
  ref int         frame_r[][],
  ref int         frame_g[][],
  ref int         frame_b[][]
);
  int    fd;
  string line;
  int code;
  int    x, y;
  int    r_val, g_val, b_val;

  fd = $fopen(filename, "r");
  if (fd == 0) begin
     $display("ERROR: Cannot open file %s", filename);
     $finish;
  end

  if (!$fgets(line, fd)) begin
     $display("ERROR: Empty file %s", filename);
     $finish;
  end

  while (!$feof(fd)) begin
     if (!$fgets(line, fd)) break;
     if (line.len() == 0 || line.substr(0,1) == "#") continue;
     code = $sscanf(line, "%d %d %h %h %h", x, y, r_val, g_val, b_val);
     if (code != 5) begin
        $display("ERROR: Malformed line: %s", line);
        $finish;
     end
     frame_r[x][y] = r_val;
     frame_g[x][y] = g_val;
     frame_b[x][y] = b_val;
  end

  $fclose(fd);
endfunction

function automatic void readPixelTxt_1d_in(
  string                  filename,
  ref in_pixel_t          frame[][]    
);
  int        fd;
  string     line;
  int        code;
  int        x, y;
  in_pixel_t val;                    

  fd = $fopen(filename, "r");
  if (fd == 0) begin
     $display("ERROR: Cannot open file %s", filename);
     $finish;
  end

  if (!$fgets(line, fd)) begin
     $display("ERROR: Empty file %s", filename);
     $finish;
  end

  while (!$feof(fd)) begin
     if (!$fgets(line, fd)) break;

     if (line.len() == 0 || line.substr(0,1) == "#") continue;

     code = $sscanf(line, "%d %d %h", x, y, val);
     if (code != 3) begin
        $display("ERROR: Malformed line: %s", line);
        $finish;
     end

     frame[x][y] = val;
  end

  $fclose(fd);
endfunction

function automatic void readPixelTxt_1d_out(
  string                    filename,
  ref out_pixel_t           frame[][]    
);
  int         fd;
  string      line;
  int         code;
  int         x, y;
  out_pixel_t val;                    

  fd = $fopen(filename, "r");
  if (fd == 0) begin
     $display("ERROR: Cannot open file %s", filename);
     $finish;
  end

  if (!$fgets(line, fd)) begin
     $display("ERROR: Empty file %s", filename);
     $finish;
  end

  while (!$feof(fd)) begin
     if (!$fgets(line, fd)) break;

     if (line.len() == 0 || line.substr(0,1) == "#") continue;

     code = $sscanf(line, "%d %d %h", x, y, val);
     if (code != 3) begin
        $display("ERROR: Malformed line: %s", line);
        $finish;
     end

     frame[x][y] = val;
  end

  $fclose(fd);
endfunction



endpackage : imageProcessPkg
`endif