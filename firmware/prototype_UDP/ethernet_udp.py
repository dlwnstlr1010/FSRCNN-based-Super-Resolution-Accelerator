#!/usr/bin/env python3
"""
ethernet_udp.py (v3 - Optimized)
──────────────────────────────────────────────
- TX: 1280x720 Cam -> (320x180 YYYX UDP) + (320x180 YCrCb) + (1280x720 CrCb)
- RX: 1280x720 Y (from Board)
- DISP: RX/TX 동기화 (Queue), CPU 부담 최소화 (Resize 제거)
- 종료: 'q'
"""
import socket, struct, math, cv2, numpy as np
import threading, time
from collections import OrderedDict
from queue import Queue

BOARD_IP      = "192.168.1.20"
BOARD_RX_PORT = 6001   # 보드 입력
HOST_RX_PORT  = 6002   # 호스트 수신(보드 출력)

# C 코드(echo_udp.c)의 정의와 일치
C_MAGIC     = 0x55AA
C_KIND_IN   = 1  # Host -> Board
C_KIND_OUT  = 2  # Board -> Host

CHUNK         = 1400

IN_W, IN_H, IN_BPP   = 320, 180, 4
IN_FRAME_BYTES       = IN_W * IN_H * IN_BPP
OUT_W, OUT_H, OUT_BP = 1280, 720, 1
OUT_FRAME_BYTES      = OUT_W * OUT_H * OUT_BP

TARGET_TX_FPS   = 63 # TX/RX 목표 FPS
FRAME_TIMEOUT_S = 0.080
MAX_PENDING     = 4

# 소켓
tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)

rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
rx_sock.bind(("", HOST_RX_PORT))    

stop_flag = threading.Event()
tx_fps = 0
rx_fps = 0
disp_fps = 0

# --- 글로벌 변수 (Display Thread 공유용) ---
in_lock = threading.Lock()
# 왼쪽 프리뷰용 (320x180)
latest_input_y = None
latest_input_cr = None
latest_input_cb = None
# 오른쪽 출력용 (1280x720) - 미리 리사이즈된 Cb, Cr
latest_cr_big = None
latest_cb_big = None

# --- RX -> DISP 동기화용 큐 ---
frame_q = Queue(maxsize=2) # 실시간성 유지를 위해 버퍼 최소화


def make_hdr(fid, cid, total, paylen):
    # C 코드의 udp_hdr_t 구조체와 네트워크 바이트 순서(>)에 맞게 수정
    return struct.pack(">H H I H H H H", 
                       C_MAGIC, C_KIND_IN, fid, cid, total, paylen, 0)

def open_camera():
    idx = 3 # <-- 카메라 인덱스를 3번으로 고정
    for be in (cv2.CAP_MSMF, cv2.CAP_DSHOW, 0):
        cap = cv2.VideoCapture(idx, be) if be != 0 else cv2.VideoCapture(idx)
        if cap is not None and cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_FPS, TARGET_TX_FPS)
            
            # 설정 확인
            w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
            if w == 1280 and h == 720:
                print(f"[INFO] Camera index {idx} opened (1280x720 @ {TARGET_TX_FPS} FPS).")
                return cap
            else:
                print(f"[INFO] Camera {idx} opened but did not accept 1280x720.")
                cap.release()
            
    print(f"[ERROR] Could not open camera {idx} with 1280x720 resolution.")
    return None

def make_test_frame(t):
    h, w = 1080, 1920
    img = np.zeros((h, w, 3), np.uint8)
    grad = (np.linspace(0, 255, w, dtype=np.uint8)[None, :]).repeat(h, axis=0)
    img[..., 1] = grad
    img[..., 2] = ((grad.T + (t * 3)) % 256).T
    cv2.putText(img, "TEST", (w//2 - 120, h//2), cv2.FONT_HERSHEY_SIMPLEX, 2, (255,255,255), 4)
    return img

def tx_thread():
    global tx_fps, latest_input_y, latest_input_cr, latest_input_cb
    global latest_cr_big, latest_cb_big
    
    cap = open_camera()
    use_cam = cap is not None
    print("[TX] cam:", use_cam)

    frame_id = 0
    last_fps_t = time.time()
    sent = 0
    frame_interval = 1.0 / max(1, TARGET_TX_FPS)

    while not stop_flag.is_set():
        t0 = time.time()

        if use_cam:
            ret, frame = cap.read() # frame은 1280x720
            if not ret:
                use_cam = False 
                frame = make_test_frame(frame_id) # 1920x1080
        else:
            frame = make_test_frame(frame_id) # 1920x1080

        # BGR -> YCrCb
        ycc = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        y, cr, cb = cv2.split(ycc) 

        # 보드 전송용/왼쪽 프리뷰용 (320x180)
        y_s  = cv2.resize(y,  (IN_W, IN_H), interpolation=cv2.INTER_AREA)
        cr_s = cv2.resize(cr, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
        cb_s = cv2.resize(cb, (IN_W, IN_H), interpolation=cv2.INTER_AREA)
        
        # 오른쪽 디스플레이용 Cb, Cr (1280x720)
        cr_big_pre = None
        cb_big_pre = None
        
        current_frame_shape = y.shape[:2] # (H, W)
        
        if current_frame_shape == (OUT_H, OUT_W): # (720, 1280)
            # 카메라가 1280x720으로 켜져있으면 원본을 그대로 사용
            cr_big_pre = cr 
            cb_big_pre = cb
        else:
            # 테스트 프레임(1920x1080)이면 1280x720으로 리사이즈
            cr_big_pre = cv2.resize(cr, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
            cb_big_pre = cv2.resize(cb, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)

        with in_lock:
            # 왼쪽 프리뷰용 (320x180)
            latest_input_y = y_s.copy()
            latest_input_cr = cr_s.copy()
            latest_input_cb = cb_s.copy()
            # 오른쪽 디스플레이용 (1280x720) - *가장 중요*
            latest_cr_big = cr_big_pre.copy()
            latest_cb_big = cb_big_pre.copy()

        # 보드로는 YYYX 포맷 전송 (320x180 Y채널)
        x = np.zeros_like(y_s, dtype=np.uint8)
        packed = cv2.merge([y_s, y_s, y_s, x])

        data = packed.tobytes()
        total = (len(data) + CHUNK - 1) // CHUNK
        off = 0
        for cid in range(total):
            payload = data[off:off+CHUNK]
            off += len(payload)
            hdr = make_hdr(frame_id, cid, total, len(payload))
            tx_sock.sendto(hdr + payload, (BOARD_IP, BOARD_RX_PORT))

        frame_id += 1
        sent += 1

        now = time.time()
        if now - last_fps_t >= 1.0:
            tx_fps = sent
            sent = 0
            last_fps_t = now

        dt = time.time() - t0
        if dt < frame_interval:
            time.sleep(frame_interval - dt)

    if cap is not None:
        cap.release()
    print("[TX] End.")

def rx_thread():
    global rx_fps
    pending = OrderedDict()
    last_fps_t = time.time()
    got = 0
    latest_fid = -1

    while not stop_flag.is_set():
        try:
            pkt, _ = rx_sock.recvfrom(2048)
        except Exception:
            continue
        if len(pkt) < 16:
            continue

        try:
            magic, kind, fid, cid, total, paylen, _ = struct.unpack(">H H I H H H H", pkt[:16])
        except struct.error:
            continue

        if magic != C_MAGIC or kind != C_KIND_OUT:
            continue
            
        payload = pkt[16:16+paylen]
        now = time.time()

        # 타임아웃/누적 억제
        to_del = [pfid for pfid, st in pending.items() if now - st["birth"] > FRAME_TIMEOUT_S]
        for pfid in to_del:
            pending.pop(pfid, None)
        while len(pending) >= MAX_PENDING:
            pending.popitem(last=False)
        for pfid in list(pending.keys()):
            if pfid < latest_fid:
                pending.pop(pfid, None)

        st = pending.get(fid)
        if st is None:
            if fid < latest_fid:
                continue
            st = {"birth": now, "total": total, "chunks": {}, "got": 0}
            pending[fid] = st

        if cid not in st["chunks"]:
            st["chunks"][cid] = payload
            st["got"] += 1

        if st["got"] == st["total"]:
            try:
                data = b"".join(st["chunks"][i] for i in range(st["total"]))
            except KeyError:
                pending.pop(fid, None)
                continue

            if len(data) == OUT_FRAME_BYTES:
                y = np.frombuffer(data, dtype=np.uint8).reshape(OUT_H, OUT_W)
                
                # DISP 스레드로 프레임 전송
                try:
                    if frame_q.full():
                        frame_q.get_nowait() # 큐가 꽉 차면(DISP가 느리면) 오래된 프레임 제거
                    frame_q.put_nowait(y)    # 새 프레임 추가
                except:
                    pass
                
                latest_fid = max(latest_fid, fid)
                got += 1

            pending.pop(fid, None)

        if now - last_fps_t >= 1.0:
            rx_fps = got
            got = 0
            last_fps_t = now

    print("[RX] End.")

def display_thread():
    global disp_fps
    cv2.namedWindow("UDP Dual View", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("UDP Dual View", 1280, 360)

    last_fps_t = time.time()
    shown = 0
    
    while not stop_flag.is_set():
        
        # 큐에서 새 프레임(y_fpga)이 올 때까지 대기
        try:
            y_fpga = frame_q.get(timeout=1) 
        except:
            continue # 새 프레임 없으면 다시 대기
        
        # 프레임 버퍼 초기화
        left_bgr = np.zeros((OUT_H, OUT_W, 3), np.uint8)
        right_bgr = np.zeros((OUT_H, OUT_W, 3), np.uint8)

        # 1. tx_thread로부터 Cb/Cr 데이터 가져오기
        y_ref, cr_ref, cb_ref = None, None, None
        cr_big, cb_big = None, None
        
        with in_lock:
            # NoneType 에러 방지를 위해 모든 변수 확인
            if (latest_input_y is not None and 
                latest_input_cr is not None and 
                latest_input_cb is not None and 
                latest_cr_big is not None and 
                latest_cb_big is not None):
                
                # 왼쪽 프리뷰용 (320x180)
                y_ref = latest_input_y.copy()
                cr_ref = latest_input_cr.copy()
                cb_ref = latest_input_cb.copy()
                # 오른쪽 디스플레이용 (1280x720)
                cr_big = latest_cr_big.copy()
                cb_big = latest_cb_big.copy()

        # 3. 데이터가 모두 준비된 경우에만 그리기
        if y_ref is not None:
            # 3a. 왼쪽 입력 프리뷰 그리기 (CPU 부하 발생 지점 1)
            left_bgr_small = cv2.cvtColor(cv2.merge([y_ref, cr_ref, cb_ref]), cv2.COLOR_YCrCb2BGR)
            left_bgr = cv2.resize(left_bgr_small, (OUT_W, OUT_H), interpolation=cv2.INTER_NEAREST)

            # 3b. 오른쪽 FPGA 출력 그리기 (CPU 부하 발생 지점 2)
            # (y_fpga는 큐에서, cr_big/cb_big은 전역변수에서)
            if cr_big is not None:
                # *중요*: 리사이즈 없이 바로 Cb/Cr을 사용 (최적화 완료)
                right_bgr = cv2.cvtColor(cv2.merge([y_fpga, cr_big, cb_big]), cv2.COLOR_YCrCb2BGR)
        
        # 4. 최종 결합 및 표시 (CPU 부하 발생 지점 3)
        combined = np.hstack([left_bgr, right_bgr])
        cv2.putText(combined, f"TX:{tx_fps:3d}  RX:{rx_fps:3d}  DISP:{disp_fps:3d}",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2)
        cv2.imshow("UDP Dual View", combined)

        shown += 1
        now = time.time()
        if now - last_fps_t >= 1.0:
            disp_fps = shown
            shown = 0
            last_fps_t = now

        if cv2.waitKey(1) & 0xFF == ord('q'):
            stop_flag.set()
            break

    cv2.destroyAllWindows()
    print("[DISP] End.")

if __name__ == "__main__":
    print("[INFO] Starting ethernet_udp.py (v3 - Optimized)...")
    th_tx = threading.Thread(target=tx_thread, daemon=True)
    th_rx = threading.Thread(target=rx_thread, daemon=True)
    th_dp = threading.Thread(target=display_thread, daemon=True)
    th_tx.start(); th_rx.start(); th_dp.start()

    try:
        while not stop_flag.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_flag.set()

    print("[INFO] Exit.")