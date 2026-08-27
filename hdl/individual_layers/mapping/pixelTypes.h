// common/pixelTypes.h
`ifndef PIXELTYPES_H
`define PIXELTYPES_H

// NPIX: Number of bytes per pixel (default is 12 ?�� 96 bits)
  parameter int IN_NPIX = 12;
  parameter int OUT_NPIX = 12;

// 8×NPIX-bit vector type
  typedef logic [8*IN_NPIX-1:0] in_pixel_t;
  typedef logic [8*OUT_NPIX-1:0] out_pixel_t;

`endif // PIXELTYPES_H
