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

See [`mediamtx/README.md`](../../mediamtx/README.md).

## Live voice recognition (with RTSP A/V)

Whisper runs on the host. While **cam_sense** is streaming, recognition taps **RTSP audio** (does not steal Sense `/audio` from MediaMTX).

```bash
# Terminal 1 — picture + sound for VLC
SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh

# Terminal 2 — live captions (same audio)
./scripts/sense_whisper_live.sh
# same as: --rtsp rtsp://10.128.93.23:8554/cam_sense
```

VLC: `rtsp://10.128.93.23:8554/cam_sense` (TCP). Speak → text prints in terminal 2.

Direct `/audio` only if MediaMTX is **not** using Sense audio:

```bash
./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio
```

Default model `small.en`. Ctrl+C to stop.

## Host preview (USB)

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```
