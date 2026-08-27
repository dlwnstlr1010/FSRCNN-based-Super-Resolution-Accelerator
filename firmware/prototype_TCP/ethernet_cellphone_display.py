#!/usr/bin/env python3
"""
ethernet_video_online_dual_color_trace_cam_ycbcr_dualmode.py
──────────────────────────────────────────────
FPGA 실시간 Super-Resolution 스트리밍 뷰어 (듀얼 모드)
- RAW 모드: 보드가 '헤더 없음' 고정 포맷일 때 호환(기본)
  * 송신: 320x180 BGRX(Y,Y,Y,0) 그대로
  * 수신: 1280x720 Y(그레이) 고정 길이 스트림
  * 표시: PIPELINE_DELAY로 대략 정렬

- ID 모드: 보드가 '헤더+frame_id' 프로토콜 지원 시 완전 동기화
  * 헤더: MAGIC='YHDR'(4) + frame_id(u32) + payload_len(u32)
  * 송신: 헤더 + 320x180 BGRX
  * 수신: 헤더 + 1280x720 Y
  * 표시: 동일 frame_id로 정확 매칭

종료: 창 선택 후 'q' 키
"""

import socket
import threading
import numpy as np
import cv2
import time
import struct
from collections import deque
from queue import Queue

# ---- 네트워크 설정 ----
IP = "192.168.1.20"
PORT = 6001
CHUNK = 65536
SOCK_TIMEOUT = 20
TCP_SNDBUF = 512 * 1024
TCP_RCVBUF = 512 * 1024

# ---- 프레임 포맷 ----
IN_W, IN_H, IN_BPP = 320, 180, 4           # TX: BGRX(Y,Y,Y,0)
IN_FRAME_BYTES = IN_W * IN_H * IN_BPP

OUT_W, OUT_H, RX_BPP = 1280, 720, 1        # RX: Y(8bpp)
RX_FRAME_BYTES = OUT_W * OUT_H * RX_BPP

# ---- ID 모드 헤더 ----
MAGIC = b'YHDR'
HDR_FMT = '<4sII'  # magic(4), frame_id(uint32), payload_len(uint32)
HDR_SIZE = struct.calcsize(HDR_FMT)

# ---- 스레드/큐/상태 ----
stop_flag = threading.Event()
frame_q = Queue(maxsize=256)      # RAW: payload(bytes), ID: (fid, payload)

# RAW 모드용 입력 큐(색 복원 원본 유지)
input_q = deque(maxlen=64)        # (y_small, cr_small, cb_small)

# ID 모드용 색 버퍼(frame_id → (y_small, cr_small, cb_small))
frame_id_tx = 0
color_buf = {}
color_buf_order = deque()
COLOR_BUF_MAX = 512

# 표시용 FPS
tx_fps = 0
rx_fps = 0
disp_fps = 0


# ─────────────────────────────
# 공통: 카메라 캡처 → Y/Cr/Cb, BGRX(Y,Y,Y,0) 페이로드 제작
# ─────────────────────────────
def capture_and_build_bgrx(cap):
    ret, frame = cap.read()
    if not ret:
        return None, None

    ycbcr = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycbcr)

    y_small  = cv2.resize(y,  (IN_W, IN_H), interpolation=cv2.INTER_AREA)
    cr_small = cv2.resize(cr, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
    cb_small = cv2.resize(cb, (IN_W, IN_H), interpolation=cv2.INTER_AREA)

    x = np.zeros((IN_H, IN_W), dtype=np.uint8)
    frame_bgrx = cv2.merge([y_small, y_small, y_small, x])
    payload = frame_bgrx.tobytes()
    return (y_small, cr_small, cb_small), payload


# ─────────────────────────────
# RAW 모드: TX (헤더 없음)
# ─────────────────────────────
def tx_thread_raw(sock, cam_index=1, target_fps_cap=0):
    global tx_fps, input_q

    cap = cv2.VideoCapture(cam_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, IN_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, IN_H)
    if target_fps_cap > 0: cap.set(cv2.CAP_PROP_FPS, target_fps_cap)

    print(f"[TX-RAW] Webcam {cam_index} started (send raw BGRX {IN_W}x{IN_H})")

    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            ycb, payload = capture_and_build_bgrx(cap)
            if ycb is None:
                continue

            sock.sendall(payload)       # 헤더 없이 보냄
            input_q.append(ycb)         # 색 복원용 큐에 저장(최신 유지)

            frame_count += 1
            if time.time() - t_start >= 1.0:
                tx_fps = frame_count
                frame_count = 0
                t_start = time.time()
    except Exception as e:
        print("[TX-RAW ERROR]", e)
    finally:
        cap.release()
        print("[TX-RAW] Webcam stream ended.")


# ─────────────────────────────
# RAW 모드: RX (헤더 없음, 고정 길이 슬라이싱)
# ─────────────────────────────
def rx_thread_raw(sock):
    global rx_fps
    buf = bytearray()
    frame_count = 0
    t_start = time.time()
    print("[RX-RAW] Receiving raw Y frames without header")

    try:
        while not stop_flag.is_set():
            chunk = sock.recv(CHUNK)
            if not chunk:
                break
            buf.extend(chunk)

            while len(buf) >= RX_FRAME_BYTES:
                frame = bytes(buf[:RX_FRAME_BYTES])
                del buf[:RX_FRAME_BYTES]

                try:
                    frame_q.put_nowait(frame)
                except:
                    # 큐가 꽉 찼으면 하나 버리고 재시도
                    try:
                        frame_q.get_nowait()
                        frame_q.put_nowait(frame)
                    except:
                        pass
                frame_count += 1

            if time.time() - t_start >= 1.0:
                rx_fps = frame_count
                frame_count = 0
                t_start = time.time()

    except Exception as e:
        print("[RX-RAW ERROR]", e)
    finally:
        stop_flag.set()
        print("[RX-RAW] End.")


# ─────────────────────────────
# ID 모드: TX (헤더 + frame_id)
# ─────────────────────────────
def tx_thread_id(sock, cam_index=1, target_fps_cap=0):
    global tx_fps, frame_id_tx, color_buf, color_buf_order

    cap = cv2.VideoCapture(cam_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, IN_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, IN_H)
    if target_fps_cap > 0: cap.set(cv2.CAP_PROP_FPS, target_fps_cap)

    print(f"[TX-ID] Webcam {cam_index} started (header+frame_id)")

    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            ycb, payload = capture_and_build_bgrx(cap)
            if ycb is None:
                continue

            frame_id_tx = (frame_id_tx + 1) & 0xFFFFFFFF
            hdr = struct.pack(HDR_FMT, MAGIC, frame_id_tx, len(payload))
            sock.sendall(hdr)
            sock.sendall(payload)

            color_buf[frame_id_tx] = ycb
            color_buf_order.append(frame_id_tx)
            while len(color_buf_order) > COLOR_BUF_MAX:
                old = color_buf_order.popleft()
                color_buf.pop(old, None)

            frame_count += 1
            if time.time() - t_start >= 1.0:
                tx_fps = frame_count
                frame_count = 0
                t_start = time.time()

    except Exception as e:
        print("[TX-ID ERROR]", e)
    finally:
        cap.release()
        print("[TX-ID] Webcam stream ended.")


# ─────────────────────────────
# ID 모드: RX (헤더 + frame_id)
# ─────────────────────────────
def rx_thread_id(sock):
    global rx_fps
    buf = bytearray()
    frame_count = 0
    t_start = time.time()
    print("[RX-ID] Receiving frames with header+frame_id")

    try:
        while not stop_flag.is_set():
            chunk = sock.recv(CHUNK)
            if not chunk:
                break
            buf.extend(chunk)

            while True:
                if len(buf) < HDR_SIZE:
                    break

                magic, fid, plen = struct.unpack(HDR_FMT, buf[:HDR_SIZE])

                if magic != MAGIC:
                    # MAGIC 재동기화
                    del buf[0]
                    continue

                if len(buf) < HDR_SIZE + plen:
                    break

                del buf[:HDR_SIZE]
                payload = bytes(buf[:plen])
                del buf[:plen]

                if len(payload) != RX_FRAME_BYTES:
                    # (보드가 다른 포맷을 보냈을 때) 폐기
                    continue

                try:
                    frame_q.put_nowait((fid, payload))
                except:
                    try:
                        frame_q.get_nowait()
                        frame_q.put_nowait((fid, payload))
                    except:
                        pass

                frame_count += 1

            if time.time() - t_start >= 1.0:
                rx_fps = frame_count
                frame_count = 0
                t_start = time.time()

    except Exception as e:
        print("[RX-ID ERROR]", e)
    finally:
        stop_flag.set()
        print("[RX-ID] End.")


# ─────────────────────────────
# 디스플레이: RAW 모드(PIPELINE_DELAY) / ID 모드(frame_id 매칭)
# ─────────────────────────────
def display_thread(mode, target_fps=0, pipeline_delay=1):
    global disp_fps

    FRAME_INTERVAL = 1.0 / target_fps if target_fps > 0 else 0.0
    last_t = time.time()

    title = f"FPGA Input | FPGA Output (mode={mode})"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 1280, 360)

    frame_count = 0
    t_start = time.time()

    try:
        while not stop_flag.is_set():
            try:
                item = frame_q.get(timeout=1)
            except:
                continue

            if mode == 'raw':
                # item: payload(bytes)
                payload = item
                y_fpga = np.frombuffer(payload, dtype=np.uint8).reshape((OUT_H, OUT_W))

                # 입력 색 프레임: N프레임 전(파이프라인 지연 추정)
                if len(input_q) > 0:
                    idx = max(0, len(input_q) - 1 - pipeline_delay)
                    y_in, cr_in, cb_in = list(input_q)[idx]
                    input_disp = cv2.cvtColor(cv2.merge([y_in, cr_in, cb_in]), cv2.COLOR_YCrCb2BGR)
                    input_disp = cv2.resize(input_disp, (OUT_W, OUT_H), interpolation=cv2.INTER_NEAREST)
                    cr_big = cv2.resize(cr_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                    cb_big = cv2.resize(cb_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                    merged = cv2.merge([y_fpga, cr_big, cb_big])
                    fpga_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
                else:
                    input_disp = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
                    fpga_bgr = cv2.cvtColor(y_fpga, cv2.COLOR_GRAY2BGR)

                combined = np.hstack([input_disp, fpga_bgr])
                overlay = f"TX: {tx_fps} | RX: {rx_fps} | DISP: {disp_fps}"
                cv2.putText(combined, overlay, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2)

            else:  # mode == 'id'
                # item: (fid, payload)
                fid, payload = item
                y_fpga = np.frombuffer(payload, dtype=np.uint8).reshape((OUT_H, OUT_W))

                ycb = color_buf.pop(fid, None)
                if ycb is not None:
                    try:
                        color_buf_order.remove(fid)
                    except ValueError:
                        pass
                    y_in, cr_in, cb_in = ycb
                    input_disp = cv2.cvtColor(cv2.merge([y_in, cr_in, cb_in]), cv2.COLOR_YCrCb2BGR)
                    input_disp = cv2.resize(input_disp, (OUT_W, OUT_H), interpolation=cv2.INTER_NEAREST)
                    cr_big = cv2.resize(cr_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                    cb_big = cv2.resize(cb_in, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
                    merged = cv2.merge([y_fpga, cr_big, cb_big])
                    fpga_bgr = cv2.cvtColor(merged, cv2.COLOR_YCrCb2BGR)
                else:
                    # 대응 입력이 없으면 임시 표시
                    input_disp = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
                    fpga_bgr = cv2.cvtColor(y_fpga, cv2.COLOR_GRAY2BGR)

                combined = np.hstack([input_disp, fpga_bgr])
                overlay = f"TX: {tx_fps} | RX: {rx_fps} | DISP: {disp_fps} | FID: {fid}"
                cv2.putText(combined, overlay, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2)

            cv2.imshow(title, combined)

            frame_count += 1
            if time.time() - t_start >= 1.0:
                disp_fps = frame_count
                frame_count = 0
                t_start = time.time()

            if target_fps > 0:
                now = time.time()
                dt = now - last_t
                if dt < FRAME_INTERVAL:
                    time.sleep(FRAME_INTERVAL - dt)
                last_t = time.time()

            if cv2.waitKey(1) & 0xFF == ord('q'):
                stop_flag.set()
                break

    except Exception as e:
        print("[DISPLAY ERROR]", e)
    finally:
        cv2.destroyAllWindows()
        print("[DISPLAY] End.")


# ─────────────────────────────
# 메인
# ─────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["raw", "id"], default="raw",
                    help="프로토콜 모드: raw(헤더 없음, 보드 레거시) | id(헤더+frame_id)")
    ap.add_argument("--fps", type=int, default=0, help="디스플레이 타겟 FPS(0=무제한)")
    ap.add_argument("--cam", type=int, default=1, help="웹캠 인덱스")
    ap.add_argument("--delay", type=int, default=1, help="RAW 모드에서 파이프라인 지연 프레임 수")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, TCP_RCVBUF)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, TCP_SNDBUF)
    sock.settimeout(SOCK_TIMEOUT)

    print(f"[INFO] Connecting to {IP}:{PORT} ...")
    sock.connect((IP, PORT))
    print("[INFO] Connected.")

    if args.mode == "raw":
        tx = threading.Thread(target=tx_thread_raw, args=(sock, args.cam, 0), daemon=True)
        rx = threading.Thread(target=rx_thread_raw, args=(sock,), daemon=True)
        disp = threading.Thread(target=display_thread, args=("raw", args.fps, args.delay), daemon=True)
    else:
        tx = threading.Thread(target=tx_thread_id, args=(sock, args.cam, 0), daemon=True)
        rx = threading.Thread(target=rx_thread_id, args=(sock,), daemon=True)
        disp = threading.Thread(target=display_thread, args=("id", args.fps, 0), daemon=True)

    tx.start()
    rx.start()
    disp.start()

    try:
        while not stop_flag.is_set():
            time.sleep(0.05)
    except KeyboardInterrupt:
        stop_flag.set()

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except:
        pass
    sock.close()
    print("[INFO] Connection closed.")


if __name__ == "__main__":
    main()
