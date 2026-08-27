#!/usr/bin/env python3
"""
ethernet_video_online_dual_color_trace_cam_ycbcr.py
──────────────────────────────────────────────
FPGA 실시간 Super-Resolution 스트리밍 뷰어 (Y채널 처리 + 색 복원)
- 카메라 입력: 1920x1080 (MJPG)
- 송신: Y채널만 320x180 (BGRX32)
- 수신: FPGA 출력 1280x720 (Y)
- 표시: 좌측 입력(축소), 우측 FPGA출력 + 색 복원 + FPS 오버레이
──────────────────────────────────────────────
종료: 창 선택 후 'q' 키
"""

import socket
import threading
import numpy as np
import cv2
import time
from pathlib import Path
from queue import Queue

# ---- 설정 ----
IP = "192.168.1.20"
PORT = 6001
IN_W, IN_H, IN_BPP = 320, 180, 4
IN_FRAME_BYTES = IN_W * IN_H * IN_BPP
OUT_W, OUT_H, RX_BPP = 1280, 720, 1
RX_FRAME_BYTES = OUT_W * OUT_H * RX_BPP
CHUNK = 65536
SOCK_TIMEOUT = 20

frame_q = Queue(maxsize=0)
stop_flag = threading.Event()
latest_input_ycbcr = None  # 색 복원용

# FPS 공유 변수
tx_fps = 0
rx_fps = 0
disp_fps = 0


# ─────────────────────────────
# 송신 스레드 (Y채널만 전송 + 입력 프레임 큐 저장)
# ─────────────────────────────
input_q = Queue(maxsize=32)
PIPELINE_DELAY = 1   # FPGA의 출력 지연 프레임 수 (필요시 1,2,3으로 조정)

def tx_thread(sock):
    global input_q, tx_fps

    cap = cv2.VideoCapture(0)
    # cap = cv2.VideoCapture(1, cv2.CAP_MSMF)
    #cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 180)
    cap.set(cv2.CAP_PROP_FPS, 100)

    target_aspect = 16 / 9
    print("[TX] Webcam 0 started (1920x1080→Y→320x180→FPGA)")

    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            ret, frame = cap.read()
            if not ret:
                continue

            # BGR → YCrCb
            ycbcr = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
            y, cr, cb = cv2.split(ycbcr)

            # 축소 (FPGA 입력)
            y_small = cv2.resize(y, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
            cr_small = cv2.resize(cr, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
            cb_small = cv2.resize(cb, (IN_W, IN_H), interpolation=cv2.INTER_AREA)

            # FPGA 전송
            x = np.zeros((IN_H, IN_W), dtype=np.uint8)
            frame_bgrx = cv2.merge([y_small, y_small, y_small, x])
            sock.sendall(frame_bgrx.tobytes())

            # FPGA 입력 큐에 저장 (최신 프레임 유지)
            if input_q.qsize() >= 32:
                input_q.get_nowait()
            input_q.put(cv2.merge([y_small, cr_small, cb_small]))

            # 4️⃣ 큐에 현재 입력 프레임 저장 (FPGA에 보낸 것 그대로)
            try:
                input_q.put_nowait(cv2.merge([y_small, cr_small, cb_small]))
            except:
                pass  # 큐가 꽉 차면 그냥 넘김 (실시간 유지용)

            # FPS 계산
            frame_count += 1
            if time.time() - t_start >= 1.0:
                tx_fps = frame_count
                frame_count = 0
                t_start = time.time()

    except Exception as e:
        print("[TX ERROR]", e)
    finally:
        cap.release()
        print("[TX] Webcam stream ended.")



# ─────────────────────────────
# 수신 스레드
# ─────────────────────────────
rx_started = threading.Event()  # RX 첫 수신 시점 동기화용 이벤트
def rx_thread(sock):
    global rx_fps
    buf = bytearray()
    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            chunk = sock.recv(CHUNK)
            if not chunk:
                break
            buf.extend(chunk)

            while len(buf) >= RX_FRAME_BYTES:
                frame = bytes(buf[:RX_FRAME_BYTES])
                del buf[:RX_FRAME_BYTES]
                frame_q.put_nowait(frame)
                frame_count += 1

                # 첫 프레임 수신 시점에 input_q 초기화 + 이벤트 트리거
                if not rx_started.is_set():
                    with input_q.mutex:
                        input_q.queue.clear()
                    rx_started.set()

            if time.time() - t_start >= 1.0:
                rx_fps = frame_count
                frame_count = 0
                t_start = time.time()

    except Exception as e:
        print("[RX ERROR]", e)
    finally:
        stop_flag.set()
        print("[RX] End.")


# ─────────────────────────────
# 디스플레이 스레드 (입력-출력 완전 동기 + 색 복원 + FPS 오버레이)
# ─────────────────────────────
def display_thread(target_fps=0):
    global input_q, disp_fps

    FRAME_INTERVAL = 1.0 / target_fps if target_fps > 0 else 0
    last_t = time.time()

    cv2.namedWindow("FPGA Input | FPGA Output (Y-color merge)", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("FPGA Input | FPGA Output (Y-color merge)", 1280, 360)

    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            try:
                frame_data = frame_q.get(timeout=1)
            except:
                continue

            y_fpga = np.frombuffer(frame_data, dtype=np.uint8).reshape((OUT_H, OUT_W))

            # ───── FPGA 출력과 맞는 'N프레임 전' 입력 프레임 가져오기 ─────
            with input_q.mutex:
                if len(input_q.queue) > PIPELINE_DELAY:
                    ycbcr_in = list(input_q.queue)[-PIPELINE_DELAY]  # N프레임 전
                elif len(input_q.queue) > 0:
                    ycbcr_in = input_q.queue[0]
                else:
                    ycbcr_in = None

            if ycbcr_in is not None:
                # 입력 표시
                input_disp = cv2.cvtColor(ycbcr_in, cv2.COLOR_YCrCb2BGR)
                input_disp = cv2.resize(input_disp, (OUT_W, OUT_H), interpolation=cv2.INTER_NEAREST)

                # 출력 색 복원
                y_in, cr_in, cb_in = cv2.split(ycbcr_in)
                cr_big = cv2.resize(cr_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                cb_big = cv2.resize(cb_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                merged = cv2.merge([y_fpga, cr_big, cb_big])
                fpga_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
            else:
                input_disp = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
                fpga_bgr = cv2.cvtColor(y_fpga, cv2.COLOR_GRAY2BGR)

            # 결합 + 표시
            combined = np.hstack([input_disp, fpga_bgr])
            text = f"TX: {tx_fps} | RX: {rx_fps} | DISP: {disp_fps}"
            cv2.putText(combined, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)

            cv2.imshow("FPGA Input | FPGA Output (Y-color merge)", combined)

            frame_count += 1
            if time.time() - t_start >= 1.0:
                disp_fps = frame_count
                frame_count = 0
                t_start = time.time()

            if target_fps > 0:
                dt = time.time() - last_t
                if dt < FRAME_INTERVAL:
                    time.sleep(FRAME_INTERVAL - dt)
                last_t = time.time()

            if cv2.waitKey(1) & 0xFF == ord('q'):
                stop_flag.set()
                break

    except Exception as e:
        print("[DISP ERROR]", e)
    finally:
        cv2.destroyAllWindows()
        print("[DISPLAY] End.")

# ─────────────────────────────
# 메인
# ─────────────────────────────
def main():
    import argparse
    global args

    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=0)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
    sock.settimeout(SOCK_TIMEOUT)

    print(f"[INFO] Connecting to {IP}:{PORT} ...")
    sock.connect((IP, PORT))
    print("[INFO] Connected.")

    tx = threading.Thread(target=tx_thread, args=(sock,), daemon=True)
    rx = threading.Thread(target=rx_thread, args=(sock,), daemon=True)
    disp = threading.Thread(target=display_thread, args=(args.fps,), daemon=True)

    tx.start()
    rx.start()
    disp.start()

    try:
        while not stop_flag.is_set():
            time.sleep(1/20)
    except KeyboardInterrupt:
        stop_flag.set()

    sock.close()
    print("[INFO] Connection closed.")


if __name__ == "__main__":
    main()
