# CameraRTSPWiFi — XIAO Sense RTSP (lab)

Flash-and-go LabPSK RTSP for MediaMTX **TCP pull** (no ffmpeg while RTSP-focused).

## Provenance

| Piece | Source in this monorepo |
|-------|-------------------------|
| Pins / XCLK / fb_count | `esp32cam-rtsp/boards/esp32cam_seeed_xiao_esp32s3_sense.json` |
| JPEG ~10, VGA, 10 fps | `esp32cam-rtsp/include/settings.h` + LabPSK stability |
| Micro-RTSP | `Micro-RTSP/` → [geeksville/Micro-RTSP](https://github.com/geeksville/Micro-RTSP) |
| MediaMTX TCP proxy | `mediamtx-repo` docs: proxy + decrease-packet-loss |

URL (same as rzeldent esp32cam-rtsp): `rtsp://<ip>:554/mjpeg/1`

## Flash

```bash
./scripts/flash_camera_rtsp.sh /dev/cu.usbmodem101
```

## Mini + watch

```bash
XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
./scripts/watch_xiao_rtsp.sh   # → rtsp://10.128.93.23:8554/cam_xiao
```

Full runbook: [`mediamtx/info.text`](../../mediamtx/info.text).
