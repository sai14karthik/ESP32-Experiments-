# LabPSK camera → MediaMTX RTSP (locked)

Ignore SoftAP / SaiPhone for lab work.

## Pipeline

```
XIAO CameraRTSPWiFi (LabPSK)  MJPEG  rtsp://<ESP>:554/mjpeg/1
        → Mini ffmpeg (H.264, 10 fps, wallclock)
        → MediaMTX
        → VLC: rtsp://<MINI_IP>:8554/cam_xiao
```

Use **MediaMTX :8554**, not the ESP `:554` URL, for a clean H.264 RTSP stream.

## Board (this Mac USB)

Flash `firmware/CameraRTSPWiFi` (LabPSK). Serial must show:

```text
WiFi OK 10.128.93.xx
RTSP: rtsp://10.128.93.xx:554/mjpeg/1
```

## Mini (every run)

```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao' || true
cd ~/Desktop/ESP32-Experiments-
git fetch origin && git checkout origin/main -- scripts/publish_xiao.sh scripts/mediamtx_run.sh mediamtx/mediamtx.yml
XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
```

Wait for `first frame OK` and **no** `discarding 4xxx frames`.

## Watch

```text
rtsp://10.128.93.23:8554/cam_xiao
```

VLC: File → Open Network. Prefer TCP if offered.

WebRTC/HLS exist but RTSP is the primary lab viewer.
