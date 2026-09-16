# MediaMTX (XIAO → RTSP)

```
XIAO :81/stream (MJPEG)
  → ffmpeg (H.264) on Mini
  → MediaMTX :8554/cam_xiao
```

S3 has no HW H.264, so the host remuxes MJPEG for VLC/colleague RTSP. HLS `:8888` is optional.

## Run (Mini)

```bash
./scripts/mediamtx_run.sh
```

Firmware: **CameraWebServerWiFi**.

## Watch

- `rtsp://127.0.0.1:8554/cam_xiao` (VLC, TCP)
- `rtsp://10.128.93.23:8554/cam_xiao`
- `http://10.128.93.23:8888/cam_xiao/`

```bash
XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
```
