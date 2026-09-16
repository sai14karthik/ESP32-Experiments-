# MediaMTX (XIAO → Mini → RTSP)

```
XIAO CameraWebServerWiFi
  http://10.128.93.25:81/stream   (MJPEG)
       │
       ▼  ffmpeg on Mini (H.264, zerolatency)
MediaMTX :8554/cam_xiao
       │
       ▼
VLC / colleague
  rtsp://10.128.93.23:8554/cam_xiao  (TCP)
```

ESP32-S3 has no HW H.264. Mini remuxes MJPEG so VLC gets a normal H.264 RTSP path.  
HLS `:8888` is optional. Prefer **TCP** end-to-end on LabPSK (not UDP).

![Architecture](esp32-mediamtx-architecture.png)

## Lab IPs

| Role | Address |
|------|---------|
| Mini (Ethernet / LabPSK) | `10.128.93.23` |
| XIAO (example) | `10.128.93.25` |

## Prerequisites (Mini)

```bash
brew install mediamtx ffmpeg
```

Firmware on the board: **CameraWebServerWiFi** (not CameraRTSPWiFi).

```bash
./scripts/flash_camera_webserver.sh /dev/cu.usbmodem101
# Serial: Camera Ready! Use 'http://10.128.93.25'
```

## Run (Mini)

```bash
./scripts/mediamtx_run.sh
```

Starts MediaMTX; `mediamtx.yml` `runOnInit` pulls `:81/stream` and publishes H.264 to `cam_xiao`.

Override ESP URL if the DHCP address changes:

```bash
XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
```

Stop: **Ctrl+C**. If `:8554` is busy: `pkill -f mediamtx`.

## Watch

| Client | URL |
|--------|-----|
| VLC on Mini | `rtsp://127.0.0.1:8554/cam_xiao` |
| Colleague (LabPSK) | `rtsp://10.128.93.23:8554/cam_xiao` |
| HLS backup | `http://10.128.93.23:8888/cam_xiao/` |

VLC: Open Network → that URL → use **TCP**; network caching ~50–100 ms.

## Config notes

- Source of truth: `mediamtx/mediamtx.yml` (runtime copy: `mediamtx.runtime.yml`, gitignored).
- HLS uses `hlsVariant: lowLatency` → `hlsSegmentCount` must be **≥ 7**.
- Current encode defaults: ~12 fps, CRF 20 / max ~2.5 Mb/s (see `runOnInit` in the yml).
- Board stream defaults: HVGA 480×320, JPEG q8, 12 fps (`CameraWebServerWiFi`).

## Alternate firmware

`CameraRTSPWiFi` (Micro-RTSP `:554/mjpeg/1`) is kept for experiments. The Mini path above expects **HTTP MJPEG** from CameraWebServerWiFi. Standalone bridge without editing the yml:

```bash
./scripts/publish_xiao.sh http://10.128.93.25:81/stream
# with MediaMTX already listening for a publisher on cam_xiao
```
