# CameraRTSPWiFi — XIAO ESP32-S3 Sense RTSP (continuous live)

Replaces flaky HTTP `/capture` polls and long-lived `:81/stream` MJPEG with
**Micro-RTSP** on the board (`:554/mjpeg/1`). Mini ffmpeg remuxes MJPEG→H.264
into MediaMTX so Safari HLS/WebRTC stay continuous (no slowly growing clock).

Based on [geeksville/Micro-RTSP](https://github.com/geeksville/Micro-RTSP)
(same idea as [rzeldent/esp32cam-rtsp](https://github.com/rzeldent/esp32cam-rtsp)).
Vendored under `Micro-RTSP/`.

## Flash (this Mac)

```bash
FQBN='esp32:esp32:XIAO_ESP32S3:PSRAM=opi,PartitionScheme=default_8MB,CDCOnBoot=default,UploadSpeed=921600'
SKETCH=firmware/CameraRTSPWiFi
PORT=/dev/cu.usbmodem1101   # adjust

arduino-cli compile --fqbn "$FQBN" \
  --library "$SKETCH/Micro-RTSP" \
  "$SKETCH"

arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
```

Serial should print:

```text
RTSP: rtsp://10.128.93.25:554/mjpeg/1
```

## MediaMTX on Mini (continuous live)

```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao' || true
cd ~/Desktop/ESP32-Experiments-   # or this repo path on Mini
XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
```

Colleague / browser (Mini LabPSK IP `10.128.93.23`):

| Protocol | URL |
|----------|-----|
| **HLS** | http://10.128.93.23:8888/cam_xiao/ |
| **WebRTC** | http://10.128.93.23:8889/cam_xiao/ |
| RTSP | `rtsp://10.128.93.23:8554/cam_xiao` |

## vs CameraWebServerWiFi

| | CameraWebServerWiFi | CameraRTSPWiFi |
|--|--|--|
| Board output | MJPEG HTTP `:81/stream` + `/capture` | RTSP `:554/mjpeg/1` |
| Mini | ffmpeg polls `/capture` (choppy HLS) | ffmpeg continuous RTSP pull |
| Use when | Quick web preview | Continuous colleague stream |

Keep `CameraWebServerWiFi` as the safe MJPEG HTTP fallback.
