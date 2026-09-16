# MediaMTX — simple RTSP (no ffmpeg)

```
ESP CameraRTSPWiFi                    MediaMTX                         VLC
rtsp://10.128.93.25:554/mjpeg/1  ──►  :8554/cam_xiao  ──►  rtsp://…/cam_xiao
```

No ffmpeg. No HLS. No WebRTC.

## 1) Board (once)

Firmware: `CameraRTSPWiFi`  
Serial must show: `RTSP: rtsp://10.128.93.25:554/mjpeg/1`

```bash
./scripts/flash_camera_rtsp.sh
```

## 2) Mini (every time)

```bash
pkill -f mediamtx 2>/dev/null; true
./scripts/mediamtx_run.sh
```

Wait for: `stream is available and online, 1 track (M-JPEG)`

## 3) Watch

VLC → Open Network Stream → **`rtsp://127.0.0.1:8554/cam_xiao`**  
(force **RTP over RTSP (TCP)**)

Colleague: **`rtsp://10.128.93.23:8554/cam_xiao`**

Do not use ffplay for this MJPEG path.
