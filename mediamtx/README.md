# MediaMTX (N× XIAO → Mini → RTSP)

```
N× CameraWebServerWiFi  http://<ip>:81/stream
        │
        ▼  ffmpeg on Mini (H.264)
MediaMTX :8554/cam_xiao, cam_xiao2, … cam_xiaoN
        │
        ▼
rtsp://10.128.93.23:8554/cam_xiao
rtsp://10.128.93.23:8554/cam_xiao2
…
```

ESP32-S3 has no HW H.264. Mini remuxes MJPEG. Prefer **TCP** on LabPSK.

![Architecture](esp32-mediamtx-architecture.png)

## Lab IPs

| Role | Address |
|------|---------|
| Mini (Ethernet / LabPSK) | `10.128.93.23` |
| XIAO (examples) | `10.128.93.25`, `10.128.93.34`, … |

## Prerequisites (Mini)

```bash
brew install mediamtx ffmpeg
```

Firmware: **CameraWebServerWiFi** (`./scripts/flash_camera_webserver.sh /dev/cu.usbmodem…`).

## Run (Mini) — N cameras

```bash
# defaults (2 lab boards):
./scripts/mediamtx_run.sh

# any N — pass MJPEG URLs:
./scripts/mediamtx_run.sh \
  http://10.128.93.25:81/stream \
  http://10.128.93.34:81/stream \
  http://10.128.93.40:81/stream

# or comma list:
XIAO_MJPEG_URLS=http://10.128.93.25:81/stream,http://10.128.93.34:81/stream \
  ./scripts/mediamtx_run.sh
```

Paths are named `cam_xiao`, `cam_xiao2`, `cam_xiao3`, …

Stop: **Ctrl+C**. Busy port: `pkill -f mediamtx`.

## Watch

| Cam | RTSP (LabPSK) | HLS |
|-----|---------------|-----|
| 1 | `rtsp://10.128.93.23:8554/cam_xiao` | `http://10.128.93.23:8888/cam_xiao/` |
| 2 | `rtsp://10.128.93.23:8554/cam_xiao2` | `http://10.128.93.23:8888/cam_xiao2/` |
| N | `rtsp://10.128.93.23:8554/cam_xiaoN` | `http://10.128.93.23:8888/cam_xiaoN/` |

VLC: Open Network → URL → **TCP**; caching ~50–100 ms.

## Config notes

- Base: `mediamtx/mediamtx.yml`; runtime paths: `mediamtx.runtime.yml` (gitignored).
- Encode defaults: ~12 fps, CRF 20 / max ~2.5 Mb/s.
- Board defaults: HVGA 480×320, JPEG q8, 12 fps.
