# CameraRTSPWiFi — XIAO Sense RTSP (lab)

LabPSK flash-and-go Micro-RTSP for MediaMTX **TCP pull**.

Full runbook: [`mediamtx/README.md`](../../mediamtx/README.md)

## Quick

```bash
./scripts/flash_camera_rtsp.sh /dev/cu.usbmodem101
# Serial: RTSP: rtsp://10.128.93.25:554/mjpeg/1

# Mini:
XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
./scripts/watch_xiao_rtsp.sh rtsp://127.0.0.1:8554/cam_xiao

# Colleague (LabPSK + VLC):
#   rtsp://10.128.93.23:8554/cam_xiao
```

## Provenance

| Piece | Source |
|-------|--------|
| Pins / PSRAM / fb_count | `esp32cam-rtsp` XIAO Sense board JSON |
| Micro-RTSP | `Micro-RTSP/` submodule (geeksville) + lab TCP/timestamp/RTP-Info fixes |
| Reference (do not flash) | Local clones `ESP32-RTSP/`, root `Micro-RTSP/` — AI-Thinker / port 8554 |
| MediaMTX TCP proxy | `mediamtx/` + `mediamtx-repo` docs |
