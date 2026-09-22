# CameraWebServerWiFiSense

XIAO **ESP32-S3 Sense** multimodal firmware: **Wi‑Fi camera + PDM mic + Wi‑Fi CSI**.

- Video: `http://<ip>:81/stream` (MJPEG)
- Audio PCM: `http://<ip>/audio` (raw **s16le**, 16 kHz, mono) for Mini ffmpeg → MediaMTX
- Mic levels: `http://<ip>/mic` (JSON)
- USB: `CSI_DATA` + `rms:` lines for `cam_mic_preview.py`

For **video-only** MediaMTX paths (`cam_xiao`), flash [`../CameraWebServerWiFi`](../CameraWebServerWiFi/) instead.

## Flash

```bash
./scripts/flash_camera_sense.sh /dev/cu.usbmodem…
```

Or Arduino IDE: open `CameraWebServerWiFiSense.ino`, board **XIAO_ESP32S3** (PSRAM), Upload.

Serial (115200/921600) should show:

- `Mic ready (PDM Sense)`
- `Camera Ready! Use 'http://…'`
- `Audio: http://…/audio`
- `# CSI …` / `CSI_DATA,…` and `rms:…`

## MediaMTX (audio + video) — N boards

On Mini (each Sense board needs this firmware + `/audio`):

```bash
# one:
SENSE_AV_URL=http://10.128.93.XX ./scripts/mediamtx_run.sh

# N:
SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.40,http://10.128.93.41 \
  ./scripts/mediamtx_run.sh
```

VLC (TCP, enable audio): `rtsp://10.128.93.23:8554/cam_sense`  
(and `cam_sense2`, `cam_sense3`, …)

Lip-sync tweak (audio usually leads JPEG video; delay audio in ms):

```bash
SENSE_AV_AUDIO_DELAY_MS=180 SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh
# if lips still early: try 250; if late: try 100
```

See [`mediamtx/README.md`](../../mediamtx/README.md).

## Host preview (USB)

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```
