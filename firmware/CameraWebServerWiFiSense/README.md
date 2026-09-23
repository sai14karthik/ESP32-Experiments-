# CameraWebServerWiFiSense

XIAO **ESP32-S3 Sense** multimodal firmware: **Wi‑Fi camera + PDM mic + Wi‑Fi CSI**.

- Video: `http://<ip>:81/stream` (MJPEG) — default **VGA 640×480**, JPEG q8 (max practical A/V on LabPSK)
- Audio PCM: `http://<ip>/audio` (raw **s16le**, 16 kHz, mono) for Mini ffmpeg → MediaMTX
- Mic levels: `http://<ip>/mic` (JSON)
- USB: `CSI_DATA` + `rms:` lines for `cam_mic_preview.py` (CSI off by default for smooth A/V)

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

## Live voice recognition (Whisper on Mini)

Local speech-to-text from the Sense mic. **Not** a cloud LLM. Runs on the Mac Mini
(`openai-whisper`, MPS when available).

### Pipeline

```
Sense  /audio (s16le 16 kHz) + :81/stream (MJPEG)
        │
        ▼  ffmpeg_sense_av.sh  (one pull from the board)
        ├─► H.264+AAC → MediaMTX :8554/cam_sense  → VLC   (smooth; remux delay OK)
        └─► raw PCM   → udp://127.0.0.1:19055     → sense_whisper_live
                                                      │
                                                      ├─ energy VAD (phrase segments)
                                                      └─ Whisper → [HH:MM:SS] text
```

- Whisper listens on the **UDP PCM tee**, not MediaMTX RTSP — no AAC/remux lag on captions.
- UDP is localhost-only so a slow Whisper client cannot stall VLC (unlike TCP backpressure).
- Sense `/audio` allows **one** HTTP client; MediaMTX owns it. Do not also `--url` while MediaMTX is up.

### Setup (Mini)

```bash
uv sync --group whisper

# Terminal 1 — restart after pulling so the PCM tee is active
SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh

# Terminal 2
./scripts/sense_whisper_live.sh
```

Healthy capture looks like:

```
[source] UDP pcm :19055 (no MediaMTX lag)
[whisper] loading turbo on mps…
[whisper] ready — speak near the Sense mic
[capture] ~16000 samples/s
[16:12:04] hello this is a test
```

VLC (separate): `rtsp://10.128.93.23:8554/cam_sense` (TCP, enable audio).

### Models (`--model`)

Default: **`turbo`** (`large-v3-turbo`) — near large-v3 quality, much faster. First download ~1.6 GB.

| Model | Use when |
|-------|----------|
| `turbo` | **default / live captions** (recommended) |
| `large-v3` | max accuracy; slower on Mini |
| `medium.en` / `small.en` | faster / lighter |
| `base.en` / `tiny.en` | quick tests |

```bash
./scripts/sense_whisper_live.sh --model turbo
./scripts/sense_whisper_live.sh --model large-v3
./scripts/sense_whisper_live.sh --model small.en
```

Needs `openai-whisper>=20240930` (`uv sync --group whisper`).

### VAD / quiet speech (`--vad-db`)

Energy gate in dBFS. **More negative = more sensitive** (quieter sounds).

| `--vad-db` | Behavior |
|------------|----------|
| `-38` | loud speech only |
| `-42` | default |
| `-48` | quieter speech |
| `-52` … `-55` | very low level |
| `-60` | often too sensitive (room noise) |

```bash
./scripts/sense_whisper_live.sh --vad-db -52
```

Speak, then pause ~0.5 s so VAD closes a segment before Whisper runs.

### Latency (what actually delays captions)

| Stage | Affects captions? |
|-------|-------------------|
| MediaMTX AAC / muxdelay | **No** (VLC only) |
| UDP PCM tee | negligible |
| VAD wait for pause | ~0.5–1 s |
| Whisper inference | **main** delay (use `turbo` to cut it) |

### Other sources (optional)

```bash
# MediaMTX not running — take Sense /audio directly
./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio

# Last resort — demux RTSP AAC (extra MediaMTX latency)
./scripts/sense_whisper_live.sh --rtsp rtsp://127.0.0.1:8554/cam_sense
```

Env: `SENSE_PCM_UDP_PORT` (default `19055`) must match `ffmpeg_sense_av.sh`.

Code: `scripts/sense_whisper_live.sh` → `firmware/tools/sense_whisper_live.py`.
Also see [`mediamtx/README.md`](../../mediamtx/README.md).

### Later (not built)

**N× Whisper streams:** VLC already has `cam_sense`, `cam_sense2`, … All boards currently share PCM UDP **19055**, so multi-board captions would collide. When asked: per-board ports (`19055+i`) + N Whisper listeners (or one labeled process).

## Host preview (USB)

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```
