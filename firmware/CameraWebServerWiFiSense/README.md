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

VLC (TCP, enable audio): `rtsp://10.128.93.13:8554/cam_sense`
(and `cam_sense2`, `cam_sense3`, …)

See [`mediamtx/README.md`](../../mediamtx/README.md).

## Live voice recognition (Whisper on Mini)

Local speech-to-text from the Sense mic. **Not** a cloud LLM.

On Apple Silicon Mini, default backend is **`mlx-whisper` (Apple Metal / MLX)** — much
faster than openai-whisper on CPU or flaky MPS. Falls back to openai-whisper only if MLX
is missing.

### Pipeline

```
Sense  /audio (s16le 16 kHz) + :81/stream (MJPEG)
        │
        ▼  ffmpeg_sense_av.sh  (one pull from the board)
        ├─► H.264+AAC → MediaMTX :8554/cam_sense  → VLC   (smooth; remux delay OK)
        └─► speech PCM → udp://127.0.0.1:19055(+n) → sense_whisper_live
                                                      │
                                                      ├─ WebRTC VAD (+ early partial ~1.5s)
                                                      └─ mlx-whisper (Metal) → text
```

- Whisper listens on the **UDP PCM tee**, not MediaMTX RTSP — no AAC/remux lag on captions.
- Per-board ports: `cam_sense` → **19055**, `cam_sense2` → **19056**, …
- Whisper branch is speech-EQ + stronger compressor (separate from RTSP AAC).
- Sense `/audio` allows **one** HTTP client; MediaMTX owns it. Do not also `--url` while MediaMTX is up.

### Setup (Mini)

```bash
uv sync --group whisper   # installs mlx-whisper + mlx-metal

# Terminal 1 — PCM tee must be active
SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.34 ./scripts/mediamtx_run.sh

# Terminal 2 — captions from the board IP you want
./scripts/sense_whisper_live.sh --ip 10.128.93.34
# ./scripts/sense_whisper_live.sh --ip 10.128.93.25
# ./scripts/sense_whisper_live.sh --ip 10.128.93.25,10.128.93.34
```

Healthy log:

```
[source] --ip 10.128.93.34 → UDP [19056] (10.128.93.34)
[backend] mlx / Metal
[capture] listening udp://127.0.0.1:19056 (10.128.93.34)
[whisper] ready on Apple Metal (MLX) …
[capture] 10.128.93.34 ~16000 samples/s
[16:12:04] [10.128.93.34] [YOU] hello this is a test  (310 ms)
```

Speaker labels (`--diarize`, on by default):

**A) ECAPA (default)** — enroll then match:

```bash
uv sync --group whisper
./scripts/sense_whisper_live.sh --ip 10.128.93.15
# 1) You speak ~3s → YOU   2) Friend ~3s → OTHER
```

**B) pyannote 3.1** — real diarization (needs Hugging Face token):

1. Create token at https://huggingface.co/settings/tokens  
2. Accept https://huggingface.co/pyannote/speaker-diarization-3.1 and `pyannote/segmentation-3.0`  
3. On Mini:

```bash
uv sync --group whisper
export HF_TOKEN=hf_...   # your token
./scripts/sense_whisper_live.sh --ip 10.128.93.15 --diarize-backend pyannote
```

First voice → **YOU**, next distinct → **OTHER**. Slower than ECAPA on CPU/MPS.

WAV enroll (ECAPA only):

```bash
./scripts/sense_whisper_live.sh --ip 10.128.93.15 \
  --enroll-you ~/Desktop/me.wav --enroll-other ~/Desktop/friend.wav
```

Take turns (overlap on one mic is hard). Disable with `--no-diarize`.

If you see `openai` / `cpu`, re-run `uv sync --group whisper`. Force MLX: `--backend mlx`.

### Models (`--model`)

Default: **`turbo`** → `mlx-community/whisper-large-v3-turbo` (best live speed/accuracy).

| Model | Use when |
|-------|----------|
| `turbo` | **default / live** (recommended) |
| `large-v3` | max accuracy; slower |
| `small.en` / `base.en` | lighter tests |

### Collar / wearable (best practice)

Physical first — software cannot fix a mic facing fabric:

1. Mic hole **toward your mouth**, not into shirt or collar fold
2. Keep the hole **clear** (no tape/case over the PDM port)
3. Prefer **upper chest / lapel**, ~15–25 cm from mouth

Audio chain (already wired):

| Stage | What |
|-------|------|
| Board | Soft AGC from pre-gain (idle ~3×, speech → ~−22 dBFS, max 10×) + DC block |
| Mini `ffmpeg_sense_av` | Speech band 80–7500 Hz + compressor → RTSP **and** Whisper UDP |
| Whisper | VAD default **−55 dBFS**, webrtc mode 1, longer preroll |

Check levels after flash (speak normally while wearing):

```bash
curl -s http://10.128.93.34/mic
# While talking: rms roughly −35 … −18 dBFS is healthy
# Silent room: often below −45
```

Then restart stream + captions on Mini:

```bash
SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.34 ./scripts/mediamtx_run.sh
./scripts/sense_whisper_live.sh --ip 10.128.93.34
```

If room noise fires captions: `--vad-db -48`. If quiet speech is missed: `--vad-db -58`.

### VAD / quiet speech (`--vad-db`)

Default **`-50`**. More negative = more sensitive.

| `--vad-db` | Behavior |
|------------|----------|
| `-42` | louder / noisy room |
| `-48` | open desk |
| `-50` | **default** (multi-board) |
| `-55` | quiet collar only |
| `-58` … `-60` | very quiet; may false-trigger |

Pause briefly after speaking (~0.3 s) so VAD closes the phrase. Captions print with inference ms.

### Latency

| Stage | Notes |
|-------|--------|
| MediaMTX | not on caption path |
| VAD hangover | ~0.3 s after you stop talking (required to know phrase end) |
| MLX turbo | main compute; usually a few hundred ms per phrase on Mini |

True zero-lag captions are impossible (must wait for end of speech). MLX + short VAD is the practical minimum on Mini.

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

**N× Whisper:** `--ip 10.128.93.25,10.128.93.34` or `--ip all`.

## Host preview (USB)

```bash
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
# LabPSK laptop (no ESP IP):
uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video
```
