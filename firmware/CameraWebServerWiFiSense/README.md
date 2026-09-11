# CameraWebServerWiFiSense

XIAO **ESP32-S3 Sense** multimodal firmware: **Wi‑Fi camera + PDM mic + Wi‑Fi CSI**.

For **MediaMTX / video-only**, flash [`../CameraWebServerWiFi`](../CameraWebServerWiFi/) instead — that sketch is unchanged camera+Wi‑Fi.

## Flash (Arduino IDE)

1. Open `CameraWebServerWiFiSense.ino` in this folder.
2. Board: **XIAO_ESP32S3** (PSRAM partition as for the camera sketch).
3. Upload.

Serial (115200) should show:

- `Mic ready (PDM Sense)`
- `Camera Ready! Use 'http://…'`
- `# CSI on …` / `# CSI ping …`
- streaming `CSI_DATA,…` and `rms:… peak:…`

## Host preview

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```

## MediaMTX (same URLs)

Either flash works for MJPEG:

```bash
./scripts/mediamtx_run.sh
./scripts/publish_xiao.sh http://<esp-ip>:81/stream
```

Prefer **CameraWebServerWiFi** when you only need video for a colleague.
