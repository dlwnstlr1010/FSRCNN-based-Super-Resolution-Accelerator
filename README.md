# FSRCNN-based Super-Resolution Accelerator on FPGA (ZCU102)

## Contributors
**이준식 (Junsik Lee)** — this repository
- Role: **Feature Extraction Layer RTL Design & Model Compression**
- Repo in Detail: [`hdl/individual_layers/feature_extraction/`](hdl/individual_layers/feature_extraction/)

**신광선 (Gwangsun Shin)**
- Role: **Mapping Layer RTL Design & Design Verification**
- Repo in Detail: https://github.com/youngyang00/FSRCNN-accelerator-mappingLayer

**조수환 (Suhwan Jo)**
- Role: **Shrinking, Expanding Layer RTL Design & TCP transport FW design**
- Repo in Detail: [FSRCNN_Layers_CustomIPs](https://github.com/suhwanJo03/FSRCNN_Layers_CustomIPs)

**이정호 (Jungho Lee)**
- Role: **Deconvolution Layer RTL Design & software reference, quantization**

## Project Overview

<img width="1140" height="416" alt="image" src="https://github.com/user-attachments/assets/71fc560f-0a40-40b2-80ef-632f68e87078" />

This project implements a hardware-accelerated FSRCNN (Fast Super-Resolution Convolutional Neural Network) on FPGA, designed to upscale low-resolution images from **320×180 to 1280×720 (×4)** and achieve **real-time processing at 60 frames per second**.

Instead of directly applying the original FSRCNN architecture, we performed **manual hyperparameter tuning** to optimize the model for FPGA deployment. By reducing the number of parameters while maintaining output quality, we created a **hardware-efficient version of FSRCNN** — **FSRCNN(d=16, s=12, m=4)** vs. the standard FSRCNN(d=56, s=12, m=4) — tailored for resource-constrained environments.

The final architecture consists of **five computational layers** — `Feature Extraction`, `Shrinking`, `Mapping`, `Expanding`, and `Deconvolution` — each implemented as a separate **Verilog RTL module**, plus a `Y_Converter` front-end. These modules are interconnected using **AXI4-Stream**, and the entire system is built as a **streaming pipeline** at the top level, enabling high-throughput, low-latency processing on the FPGA.

## 🎯 Target Model

To deploy the FSRCNN model efficiently on FPGA, we designed a lightweight and quantized version through extensive optimization and manual tuning.

### ⚙️ Experimental Setup & Evaluation Metrics
- Training conditions: Identical Epochs, Learning Rate, Batch Size, and general hyperparameters across models.
- **PSNR (Peak Signal-to-Noise Ratio)**: Measures pixel-wise distortion of the reconstructed image.
- **SSIM (Structural Similarity Index)**: Evaluates visual quality based on brightness, contrast, and structure.

### 🔧 Optimization Techniques
- Quantized weights and activations (INT8)
- Fused ReLU activation, bias removal, and zero-point elimination
- Manual tuning of number of channels and layer depth to reduce computation

### 📉 Result Highlights
- Model size reduced from **55 KB → 5 KB**
- Final model maintains **comparable performance** to the original:
    - **Original FP32**: PSNR **30.13 dB**, SSIM **0.8765**
    - **Quant-only (PTQ)**: PSNR 28.25 dB, SSIM 0.7694
    - **Final Optimized**: PSNR **30.14 dB**, SSIM **0.8610**
- Achieved ~10× size reduction with negligible quality loss

> ✅ The final target model preserves image quality while significantly reducing memory and logic usage, making it well-suited for real-time FPGA deployment.

<img width="682" height="638" alt="image" src="https://github.com/user-attachments/assets/c08095d3-8d28-4b10-bb50-76b1a1a3fe9c" />

## 🔷 IP at a Glance — `AXI4-Stream FSRCNN Upscale`
> **AXI4-Stream input (32-bit RGB-packed) → internal Y_Converter → FSRCNN 4× Upscale → AXI4-Stream output (Y 8-bit)**
> Designed to process 320×180 input and produce 1280×720 output at 60 fps on ZCU102 (300 MHz).
> Steady-state throughput: 1 pixel per clock cycle after pipeline warm-up.

<img width="571" height="326" alt="image" src="https://github.com/user-attachments/assets/26ed0da5-ffce-450e-a5ff-1d7b9b86bda3" />

### 📌 Description

This IP implements a hardware-optimized version of the FSRCNN (Fast Super-Resolution Convolutional Neural Network) architecture, structured into six RTL blocks:
**Y_Converter → Feature Extraction → Shrinking → Mapping → Expanding → Deconvolution**.
All modules are connected via **AXI4-Stream** and operate under a **shared single-clock domain** (`s_axis_aclk`), supporting real-time streaming without stalls under normal conditions.

#### Key Features:
- **Fully pipelined**: One-pixel-per-cycle throughput after warm-up.
- **Quantized computation**: All arithmetic is 8-bit or lower (INT8), optimized for DSP utilization.
- **Y-only compute path**: The FSRCNN core (Feature Extraction → Deconvolution) processes luminance (Y) only; chroma (CbCr) is reconstructed separately downstream (in this project's live demo, on the host PC, by reusing the original high-resolution chroma rather than upsampling it).
- **Timing-aware**: `EOL` (End-of-Line) and `EOF` (End-of-Frame) signals allow for shape-aware scheduling.
- **Backpressure support**: Compliant with AXI4-Stream handshake (`tvalid`/`tready`); stalls handled gracefully.

---

### 🔌 Interface Summary
The IP uses a standard AXI4-Stream interface for pixel-level input and output, with additional control signals for frame and line boundaries.

| Signal            | Direction | Width | Description |
|------------------|-----------|--------|-------------|
| `S_AXIS_tdata`    | Input     | 32     | Input pixel stream formatted as `{8'h00, Blue[7:0], Green[7:0], Red[7:0]}`. Consumed by an internal `Y_Converter` stage. |
| `S_AXIS_tvalid`   | Input     | 1      | Asserted when `S_AXIS_tdata` is valid. |
| `S_AXIS_tready`   | Output    | 1      | Asserted when the IP is ready to accept input. |
| `M_AXIS_tdata`    | Output    | 8      | Output pixel (8-bit grayscale Y) after 4× upscaling. One pixel per clock in steady state. |
| `M_AXIS_tvalid`   | Output    | 1      | Asserted when `M_AXIS_tdata` is valid. |
| `M_AXIS_tready`   | Input     | 1      | Asserted when downstream module is ready to receive output. |
| `s_axis_aclk`     | Input     | 1      | Global clock for all streaming logic. |
| `s_axis_aresetn`  | Input     | 1      | Active-low reset. Asynchronous. |
| `EOL`             | Output    | 1      | End-of-Line signal. High for the last pixel in each row. |
| `EOF`             | Output    | 1      | End-of-Frame signal. High for the bottom-right (final) pixel of the frame. Should be mapped to `M_AXIS_tlast` when integrating with strict AXI4-Stream-compliant downstream modules (e.g., VDMA, stream routers) that expect frame boundaries on `tlast`. |

---

> 💡 **Input Format & Y Conversion**:
> `S_AXIS_tdata` follows a 32-bit RGB-like format: `{8'h00, B, G, R}`.
> The **`Y_Converter` module converts this to an 8-bit luminance (Y) value using the BT.601 weighted formula**:
> `Y = 16 + (66·R + 129·G + 25·B) >> 8`
> — all three channels (R, G, B) contribute to the result; it is **not** a simple pass-through of a single channel. This Y value is what actually feeds the Feature Extraction layer.

> ⚠️ **AXI4-Stream Compliance Note**:
> This project's own layer-to-layer interconnect uses a **custom, simplified subset of AXI4-Stream**: only `tvalid`/`tready`/`tdata` are used between internal layers, and frame/line boundaries are carried on separate `EOL`/`EOF` wires computed independently by each stage's own pixel counters, rather than on `tlast`/`tuser`. This was a deliberate simplification since every stage always processes the same fixed 320×180 (or 1280×720) resolution. If wiring this IP to a standards-compliant AXI4-Stream peripheral (VDMA, AXI Stream Switch, etc.), `EOF` should be bridged to `tlast` externally.

#### Timing Notes:
- Pixel inputs must be streamed row by row (raster scan order).
- `EOL` and `EOF` are automatically generated based on internal pixel counters.
- The IP does **not** buffer entire frames — streaming latency is only pipeline depth (~100–200 cycles).

---
## 🧩 FSRCNN Core Architecture

The FSRCNN accelerator is composed of six pipelined RTL modules, each representing a distinct stage. All modules are interconnected via AXI4-Stream and synchronized under a common clock/reset domain (`s_axis_aclk`, `s_axis_aresetn`). The system is fully pipelined and supports streaming operation at one pixel per cycle after initial warm-up.

### 🔄 Data Flow

<img width="2444" height="323" alt="image" src="https://github.com/user-attachments/assets/ff2c32c6-9035-4b79-8781-153e6683d7b5" />

```
S_AXIS (32-bit RGB-packed)
   │
   ▼
[ Y_Converter ]            RGB → 8-bit Y (BT.601)
   │
   ▼
[ Feature_Extraction ]     1→16ch,  5×5 conv   ★this repo's contribution★
   │
   ▼
[ Shrinking ]              16→12ch, 1×1 conv
   │
   ▼
[ Mapping ]  ×4 chained    12→12ch, 3×3 conv
   │
   ▼
[ Expanding ]               12→16ch, 1×1 conv
   │
   ▼
[ Deconvolution ]           16→1ch,  4×4 stride-4 transposed conv (4× upscale)
   │
   ▼
M_AXIS (Upscaled Y, 1280×720)
```

- **Y_Converter**: Converts 32-bit RGB-packed input (`{00, B, G, R}`) into 8-bit luminance (Y) via BT.601 weighting. Required because the FSRCNN core processes only luminance.
- **Feature Extraction → Deconvolution**: Core FSRCNN logic, five separate Verilog/SystemVerilog AXI4-Stream RTL modules. Every conv layer shares the same design pattern: weights hardcoded as RTL parameters, DSP48 time-multiplexed reuse, adder-tree partial-sum combination, and DSP-based fixed-point requantization/clamp between stages.
- **EOF / EOL Propagation**: Rather than forwarding a single `EOF`/`EOL` down the chain, **each stage regenerates its own `EOF`/`EOL` from its own internal pixel/line counters** (a deliberate simplification valid because resolution is fixed at every stage).
- Backpressure (`tready`) is propagated stage-to-stage through small output FIFOs (`prog_full`-gated) at every boundary.

> 📎 For detailed RTL implementation of each layer module, please refer to the individual GitHub repositories linked in the **Contributors** section above.

## 🚀 Performance & Resource Utilization

All numbers below are **routed (post place-and-route)** Vivado 2022.1 reports from the fully-merged `FSRCNN_CORE_wrapper` build (all 6 layers integrated as one IP, targeting `xczu9eg` / ZCU102). Raw reports are in [`reports/fsrcnn_core_merged/`](reports/fsrcnn_core_merged/).

### 🕒 Timing Summary (300 MHz, ZCU102)

The FSRCNN core meets all timing constraints when synthesized and implemented on Xilinx ZCU102 (UltraScale+). The design is fully pipelined and operates stably at **300 MHz**.

- **Worst Negative Slack (WNS)**: +0.392 ns
- **Worst Hold Slack (WHS)**: +0.011 ns
- **Pulse Width Slack (WPWS)**: +1.124 ns
- **Status**: ✅ All user-specified timing constraints met (0 failing endpoints)

![Timing Summary](https://github.com/user-attachments/assets/14e972ec-4c45-425a-8cf7-94ae36224b08)

### 📈 Throughput

- **Frame size**: 320×180 input → 1280×720 output (4× upscaling)
- **Clock frequency**: 300 MHz
- **Throughput**: >60 frames per second (effective 1 pixel/clock in steady state, after pipeline fill)
- **Streaming model**: Fully pipelined AXI4-Stream architecture
- **Output timing**: Synchronized using per-stage `EOL` / `EOF`

### 🧮 Resource Utilization (Post-Implementation, Vivado)

| Resource         | Used    | Available | Utilization |
|------------------|---------|-----------|--------------|
| CLB LUTs         | 43,450  | 274,080   | 15.85%       |
| CLB Registers    | 29,974  | 548,160   | 5.47%        |
| Block RAM Tiles  | 107     | 912       | 11.73%       |
| DSPs             | 1,513   | 2,520     | 60.04%       |
| CARRY8           | 3,086   | 34,260    | 9.01%        |

### ⚡ Power (routed, `report_power`)

| | Power |
|---|---:|
| Total on-chip | 5.625 W |
| Dynamic | 4.973 W |
| Static | 0.652 W |

| Per-layer dynamic power | Value | Share |
|---|---:|---:|
| Y_Converter | 0.003 W | — |
| Feature Extraction | 0.499 W | — |
| Shrinking | 0.217 W | — |
| **Mapping ×4** | **3.713 W** | **74.7% of total dynamic** |
| Expanding | 0.252 W | — |
| Deconvolution | 0.282 W | — |

> 📌 The **Mapping Layer** accounts for the majority of both DSP usage (**888 of 1,513 DSPs, 58.7%**) and dynamic power (**74.7%**), due to its role handling the compute-heaviest convolutions (four chained 3×3 conv cores). This is the clear resource/power bottleneck of the design.

### 🧠 Efficiency

```
Peak GOPS = 1,513 DSPs × 2 ops/MAC × 300 MHz ≈ 907.8 GOPS (≈0.91 TOPS)
GOPS/W    = 907.8 GOPS ÷ 5.625 W (total on-chip power) ≈ 161.4 GOPS/W
```

(Vivado `report_power` here uses its default vectorless estimation — no simulation switching-activity file was supplied — so Confidence Level is reported as "Low"; this is standard practice for capstone-level FPGA power reporting. GOPS is computed with the common accelerator-literature convention of installed-compute-units × clock, counting one multiply + one add per MAC.)

## Live Demo System

- **Zynq firmware** ([`firmware/vitis_final/`](firmware/vitis_final/)): Vitis (C, bare-metal), lwIP UDP stack for host communication, AXI DMA for PL↔DDR frame transfer (`main.c`: 320×180×4B input → PL → 1280×720×1B output, double-buffered).
- **Host client** ([`firmware/prototype_UDP/ethernet_udp.py`](firmware/prototype_UDP/ethernet_udp.py)): Python + OpenCV — webcam capture, YCrCb conversion/downscale, UDP send/receive, side-by-side comparison display.
- **Demo methodology**: there is no native low-resolution camera — a live 1280×720 webcam feed is deliberately downscaled to 320×180 to synthesize a low-resolution input (the standard super-resolution evaluation technique: downsample a real HR source to get an LR/HR pair to compare against). Only the Y channel is sent through the FPGA's FSRCNN pipeline; Cr/Cb for the final display are reused directly from the original 720p frame. The display shows a side-by-side comparison: naive nearest-neighbor upscale vs. the FSRCNN accelerator output.

## Repository Structure

```
hdl/
├── individual_layers/     ← RTL extracted from each teammate's individual per-layer dev project
│   ├── y_converter/
│   ├── feature_extraction/   ★this contributor's work★
│   ├── shrinking/
│   ├── mapping/               (RTL + SystemVerilog/UVM-style verification env)
│   ├── expanding/
│   └── deconv/
└── fsrcnn_core_merged/    ← final integrated Block Design (FSRCNN_CORE.bd) that wraps
                              all 6 layers into one system, + per-core instance configs (.xci)
firmware/
├── vitis_final/           ← Zynq firmware actually used for the live demo
├── prototype_TCP/         ← earlier TCP-based dev prototype
└── prototype_UDP/         ← final UDP protocol + PC-side client (ethernet_udp.py)
reports/                   ← Vivado utilization / power / timing reports backing the numbers above
```

## Notes

- Vivado's auto-generated IP-core simulation netlists/stubs and build artifacts (`.cache`, `.gen`, `.runs`, `.sim`, from `fifo_generator`, `blk_mem_gen`, `dsp_macro`, AXI DMA, Zynq PS, etc.) are intentionally excluded — these belong to Xilinx's IP licensing and are automatically regenerated on rebuild. `hdl/fsrcnn_core_merged/ip_instances/*.xci` are the small configuration descriptors capturing how each IP core was parameterized.
- Bitstreams (`.bit`), hardware handoff files (`.xsa`), and compiled Vivado project workspaces are not included (GitHub's 100MB file-size limit, and not meaningful without the licensed tool/board anyway).
