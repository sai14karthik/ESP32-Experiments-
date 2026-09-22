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

## Live voice recognition

Whisper on the Mini uses a **raw PCM UDP tee** from `ffmpeg_sense_av` (port **19055**) — not MediaMTX RTSP — so recognition skips AAC remux delay. VLC still uses `cam_sense` as usual.

Default model: **`large-v3`** (biggest; override with `--model`).

```bash
# Terminal 1 — restart MediaMTX so the PCM tee is enabled
SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh

# Terminal 2 — captions (no MediaMTX audio path)
./scripts/sense_whisper_live.sh
# → --pcm-udp 19055 , model large-v3 (~3GB first download)
```

VLC: `rtsp://10.128.93.23:8554/cam_sense` (TCP). Speak → `[HH:MM:SS] …` in terminal 2.

Direct Sense `/audio` only if MediaMTX is **not** running:

```bash
./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio
```

Avoid `--rtsp` unless you must (extra MediaMTX latency).

## Host preview (USB)

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```
