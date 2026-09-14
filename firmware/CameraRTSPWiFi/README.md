# CameraRTSPWiFi — XIAO ESP32-S3 Sense RTSP (forever MediaMTX source)

Replaces flaky long-lived **MJPEG HTTP** (`CameraWebServerWiFi` `:81/stream`) with
**Micro-RTSP** on the board. MediaMTX **pulls** RTSP — no `publish_xiao.sh` / ffmpeg.

Based on [geeksville/Micro-RTSP](https://github.com/geeksville/Micro-RTSP) (same idea as
[Circuit.rocks ESP32-CAM RTSP](https://learn.circuit.rocks/esp32-cam-with-rtsp-video-streaming)).
Vendored under `Micro-RTSP/`.

## Flash (this Mac)

```bash
FQBN='esp32:esp32:XIAO_ESP32S3:PSRAM=opi,PartitionScheme=default_8MB,CDCOnBoot=default,UploadSpeed=921600'
SKETCH=firmware/CameraRTSPWiFi
PORT=/dev/cu.usbmodem2101   # adjust

arduino-cli compile --fqbn "$FQBN" \
  --library "$SKETCH/Micro-RTSP" \
  "$SKETCH"

arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
```

Serial should print:

```text
RTSP: rtsp://10.128.93.25:554/mjpeg/1
```

## MediaMTX on Mini (all protocols / browsers)

ESP Micro-RTSP is **MJPEG**. MediaMTX HLS/WebRTC need **H.264**, so the Mini
runs ffmpeg: ESP RTSP → H.264 → MediaMTX.

**After this firmware** (port **554**):

```bash
# Terminal A
XIAO_EXTERNAL_PUBLISH=1 ./scripts/mediamtx_run.sh

# Terminal B — one line; auto-restart on stall
while true; do
  ffmpeg -rtsp_transport tcp -i rtsp://10.128.93.25:554/mjpeg/1 \
    -an -c:v libx264 -profile:v baseline -preset veryfast -tune zerolatency \
    -pix_fmt yuv420p -bf 0 -g 12 \
    -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/cam_xiao
  echo "ffmpeg exited — restart in 2s"; sleep 2
done
```

Colleague / browser:

| Protocol | URL |
|----------|-----|
| **HLS** | http://10.128.93.23:8888/cam_xiao/ |
| **WebRTC** | http://10.128.93.23:8889/cam_xiao/ |
| RTSP | `rtsp://10.128.93.23:8554/cam_xiao` |

## vs CameraWebServerWiFi

| | CameraWebServerWiFi | CameraRTSPWiFi |
|--|--|--|
| Board output | MJPEG HTTP `:81/stream` | RTSP `:8554/mjpeg/1` |
| Mini | ffmpeg publish (dies ~1 min) | MediaMTX pulls RTSP |
| Use when | Quick web preview | Continuous colleague stream |

Keep `CameraWebServerWiFi` as the safe MJPEG fallback.
