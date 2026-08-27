import cv2, time

cap = cv2.VideoCapture(1)
if not cap.isOpened():
    print("카메라 열기 실패")
    exit()

frame_count = 0
start_time = time.time()

while True:
    ret, frame = cap.read()
    if not ret:
        break
    frame_count += 1

    # 1초마다 FPS 출력
    if time.time() - start_time >= 1.0:
        fps_measured = frame_count / (time.time() - start_time)
        print(f"현재 측정 FPS: {fps_measured:.2f}")
        frame_count = 0
        start_time = time.time()

    cv2.imshow("frame", frame)
    if cv2.waitKey(1) & 0xFF == 27:
        break

cap.release()
cv2.destroyAllWindows()