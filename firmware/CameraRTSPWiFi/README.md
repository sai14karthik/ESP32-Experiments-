# CameraRTSPWiFi — XIAO Sense Micro-RTSP (lab)

Micro-RTSP MJPEG at `rtsp://<ip>:554/mjpeg/1`.

**Current Mini / colleague path** uses **CameraWebServerWiFi** + ffmpeg → MediaMTX  
(see [`mediamtx/README.md`](../../mediamtx/README.md)). Use this sketch only if you need board-side RTSP.

## Flash

```bash
./scripts/flash_camera_rtsp.sh /dev/cu.usbmodem101
# Serial: RTSP: rtsp://10.128.93.25:554/mjpeg/1
```

Direct probe (VLC often struggles with raw MJPEG RTSP):

```bash
./scripts/watch_xiao_rtsp.sh rtsp://10.128.93.25:554/mjpeg/1
```

## Provenance

| Piece | Source |
|-------|--------|
| Pins / PSRAM / fb_count | `esp32cam-rtsp` XIAO Sense board JSON |
| Micro-RTSP | `Micro-RTSP/` submodule (geeksville) + lab TCP/timestamp/RTP-Info fixes |
