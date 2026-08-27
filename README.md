# FSRCNN 4x Video Upscaling FPGA Accelerator

FSRCNN(Fast Super-Resolution CNN) 알고리즘을 RTL로 직접 구현한 실시간 4배 영상 업스케일링 FPGA 가속기. 4인 팀 프로젝트, Xilinx Zynq UltraScale+ ZCU102(`xczu9eg`) 대상.

**본인 담당**: Feature Extraction Layer IP 설계 + 5개 레이어의 최종 시스템 통합(Integration)

## 성능 요약

| 항목 | 값 |
|---|---|
| DSP48E2 | **1,513개** (ZU9EG 전체 DSP의 60.0%) |
| 클럭 | 300MHz (타이밍 클로징 완료, WNS +0.392ns) |
| 처리량 | 907.8 GOPS (peak, ≈0.91 TOPS) |
| 전력 | 5.625W (Total on-chip, routed) |
| 전력 효율 | **161.4 GOPS/W** |

모든 수치는 Vivado 2022.1 routed(place & route 후) `report_utilization` / `report_power` 실측 결과 기준입니다. 근거는 [`reports/`](reports/) 폴더 참고.

## 모델 하이퍼파라미터

표준 FSRCNN(d=56, s=12, m=4) 대비 feature-extraction 채널을 56→16으로 줄인 **FSRCNN(d=16, s=12, m=4)** 커스텀 버전. DSP/BRAM 리소스 예산에 맞추면서 성능을 비슷하게 유지하기 위해 팀에서 직접 튜닝한 값.

## 파이프라인 구조

```
[PC 웹캠, 1280×720]
        │  BGR→YCrCb, Y/Cr/Cb 각각 320×180로 다운스케일 (인위적 LR 생성)
        │  Y채널만 UDP로 전송
        ▼
┌─────────────────────────── Zynq PL (FPGA 로직) ───────────────────────────┐
│  Y_Converter → Feature Extraction(★본인 담당★, 1→16ch, 5×5)               │
│      → Shrinking(16→12ch, 1×1) → Mapping ×4(12→12ch, 3×3 체이닝)          │
│      → Expanding(12→16ch, 1×1) → Deconv(16→1ch, 4×4 stride-4 transposed) │
└─────────────────────────────────────────────────────────────────────────┘
        │  1280×720, 8bit Y 출력, UDP로 PC에 전송
        ▼
[PC] 원본 720p 카메라의 Cr/Cb + FPGA가 만든 Y → 합성하여 화면 표시
     (좌: nearest-neighbor 확대 / 우: FSRCNN 가속기 결과 — 좌우 비교 데모)
```

실제 "네이티브 저해상도 카메라"는 없습니다 — 720p 웹캠 영상을 의도적으로 320×180으로 다운스케일해서 LR 입력을 만들고, FPGA가 이를 4배 SR로 복원한 뒤 원본과 비교하는 전형적인 super-resolution 검증 방식(HR을 다운샘플해서 LR/HR 쌍을 만드는 SR 연구의 표준 기법)입니다.

## 공통 설계 패턴 (5개 conv 레이어 전부 동일)

- 학습된 가중치를 **RTL 파라미터(상수)로 하드코딩**
- **DSP48 시간분할(time-multiplexed) 재사용** — 예: Feature Extraction은 5×5=25 MAC을 DSP 5개로 5사이클에 나눠 처리
- Adder tree로 부분합 결합
- DSP 기반 재양자화(고정소수점 shift + 0~255 clamp)로 다음 레이어에 넘길 8bit 값 생성

인터페이스는 AXI4-Stream(`tvalid`/`tready`)을 쓰되, `tlast`/`tuser` 대신 각 레이어가 자체 카운터로 `EOL`/`EOF`를 재계산하는 커스텀 방식이며, 레이어 사이마다 FIFO(`prog_full`)로 백프레셔 체인을 구성합니다.

## 레이어별 리소스 (통합 빌드 `FSRCNN_CORE_wrapper` 실측 기준)

| 레이어 | 채널(in→out) | 커널 | DSP48E2 | 동적 전력 |
|---|---|---|---:|---:|
| Y_Converter | RGB→Y | 색공간 변환(BT.601) | (미미) | 0.003 W |
| **Feature Extraction (본인 담당)** | 1→16 | 5×5 | **96** | 0.499 W |
| Shrinking | 16→12 | 1×1 | 192 | 0.217 W |
| **Mapping ×4** | 12→12 (×4) | 3×3 | **888** | **3.713 W (전체 dynamic의 74.7%)** |
| Expanding | 12→16 | 1×1 | 192 | 0.252 W |
| Deconv | 16→1 | 4×4 transpose(stride 4) | 145 | 0.282 W |
| **합계** | | | **1,513** | **4.973 W (dynamic)** |

Block RAM Tile 107개(11.73%) 사용. **Mapping Layer(3×3 conv ×4겹)가 DSP의 58.7%, 동적 전력의 74.7%를 차지하는 리소스/전력 핫스팟**입니다.

## GOPS/W 산출 근거

```
Peak GOPS = 총 DSP(1,513개) × 2 ops/MAC × 300MHz = 907.8 GOPS (≈0.91 TOPS)
GOPS/W    = 907.8 GOPS ÷ 5.625W (Total On-Chip Power) ≈ 161.4 GOPS/W
```

Vivado `report_power`의 vectorless(기본) 추정 기반이며(Confidence Level: Low — 시뮬레이션 스위칭 액티비티 파일 미사용, 학생/캡스톤 프로젝트에서 흔한 방식), GOPS 계산은 가속기 분야에서 흔히 쓰는 "설치된 연산 유닛 × 클럭" peak throughput 관례를 따릅니다.

## 데모 시스템 소프트웨어 스택

- **Zynq 펌웨어** (`firmware/vitis_final/`): Vitis(C, bare-metal), lwIP UDP 스택으로 PC와 통신, AXI DMA로 PL↔DDR 프레임 전송 (`main.c`: 320×180×4B 입력 → PL → 1280×720×1B 출력)
- **PC 클라이언트** (`firmware/prototype_UDP/ethernet_udp.py`): Python + OpenCV — 웹캠 캡처, YCrCb 변환/다운스케일, UDP 송수신, 좌우 비교 화면 표시
- **네트워크**: UDP 기반 프레임 스트리밍 (청크 분할 전송, 커스텀 헤더로 frame ID/chunk 관리)

## 저장소 구조

```
hdl/
├── individual_layers/     ← 팀원별 개별 레이어 개발 프로젝트에서 추출한 RTL
│   ├── y_converter/
│   ├── feature_extraction/   ★본인 담당★
│   ├── shrinking/
│   ├── mapping/               (RTL + UVM 스타일 검증 환경 .sv)
│   ├── expanding/
│   └── deconv/
└── fsrcnn_core_merged/    ← 6개 레이어를 하나로 묶은 최종 통합 Block Design (FSRCNN_CORE.bd)
                              + 각 서브코어 인스턴스 설정(.xci)
firmware/
├── vitis_final/           ← 라이브 데모에 실제 사용된 Zynq 펌웨어
├── prototype_TCP/         ← 개발 초기 TCP 기반 프로토타입
└── prototype_UDP/         ← 최종 UDP 프로토콜 + PC 클라이언트(ethernet_udp.py)
reports/                   ← Vivado utilization/power/timing 리포트 (위 수치의 실측 근거)
```

## 참고 사항

- Vivado가 IP 코어(`fifo_generator`, `blk_mem_gen`, `dsp_macro`, AXI DMA, Zynq PS 등)로부터 자동 생성하는 시뮬레이션 넷리스트/스텁 파일, `.cache`/`.gen`/`.runs`/`.sim` 등 빌드 산출물은 저장소에 포함하지 않았습니다(Xilinx IP 라이선스 소관이며 재빌드 시 자동 재생성됨). `hdl/fsrcnn_core_merged/ip_instances/*.xci`는 해당 IP들을 어떻게 파라미터화했는지에 대한 설정 파일입니다.
- 비트스트림(`.bit`), 하드웨어 핸드오프(`.xsa`), 압축된 Vivado 프로젝트 워크스페이스는 용량 문제로(GitHub 100MB 제한) 포함하지 않았습니다.
