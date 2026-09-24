# MediaMTX (N× XIAO → Mini → RTSP)

```
N× CameraWebServerWiFi  http://<ip>:81/stream
        │
        ▼  ffmpeg on Mini (H.264)
MediaMTX :8554/cam_xiao, cam_xiao2, … cam_xiaoN
        │
        ▼
rtsp://10.128.93.13:8554/cam_xiao
rtsp://10.128.93.13:8554/cam_xiao2
…

N× Sense A/V (CameraWebServerWiFiSense):
  :81/stream (MJPEG) + :80/audio (s16le 16 kHz)
        │
        ▼  ffmpeg_sense_av on Mini
        ├─► H.264 + AAC → MediaMTX :8554/cam_sense… → VLC
        └─► raw PCM UDP :19055 → sense_whisper_live (captions; no RTSP lag)
```

ESP32-S3 has no HW H.264. Mini remuxes MJPEG (± PCM). Prefer **TCP** on LabPSK.

![Architecture](esp32-mediamtx-architecture.png)

## Lab IPs

| Role | Address |
|------|---------|
| Mini (Ethernet / LabPSK) | `10.128.93.13` |
| XIAO (examples) | `10.128.93.25`, `10.128.93.34`, … |

## Prerequisites (Mini)

```bash
brew install mediamtx ffmpeg
```

Firmware:

- Video-only: **CameraWebServerWiFi** (`./scripts/flash_camera_webserver.sh /dev/cu.usbmodem…`)
- A/V Sense: **CameraWebServerWiFiSense** (`./scripts/flash_camera_sense.sh /dev/cu.usbmodem…`)

## Run (Mini) — N cameras (video-only)

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

## Run (Mini) — N× Sense audio + video

Flash each board with **CameraWebServerWiFiSense**. Then:

```bash
# N Sense A/V only:
SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.40,http://10.128.93.41 \
  ./scripts/mediamtx_run.sh

# single Sense (alias):
SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh

# Sense A/V + video-only cams together:
SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.40 \
  ./scripts/mediamtx_run.sh \
  http://10.128.93.34:81/stream
```

Paths: `cam_sense`, `cam_sense2`, `cam_sense3`, …  
VLC: `rtsp://10.128.93.13:8554/cam_sense` (TCP; enable **Audio track**).

### Live captions (Whisper)

While Sense A/V is up, `ffmpeg_sense_av` also tees speech-processed PCM to **`udp://127.0.0.1:19055`**. Whisper uses that tee — **not** MediaMTX RTSP — so captions skip AAC remux delay. VLC is unchanged. Collar/wearable: board soft AGC + ffmpeg speech band/compressor (see Sense README).

```bash
uv sync --group whisper   # once
# restart mediamtx_run after pulling so the tee exists
./scripts/sense_whisper_live.sh                  # UDP :19055, mlx Metal turbo
./scripts/sense_whisper_live.sh --model large-v3 # max accuracy
./scripts/sense_whisper_live.sh --vad-db -52      # quieter speech
```

Full pipeline, models, VAD, latency: [`firmware/CameraWebServerWiFiSense/README.md`](../firmware/CameraWebServerWiFiSense/README.md#live-voice-recognition-whisper-on-mini).

Stop: **Ctrl+C**. Busy port: `pkill -f mediamtx`.

## Watch

| Cam | RTSP (LabPSK) | HLS |
|-----|---------------|-----|
| video 1 | `rtsp://10.128.93.13:8554/cam_xiao` | `http://10.128.93.13:8888/cam_xiao/` |
| video N | `rtsp://10.128.93.13:8554/cam_xiaoN` | `http://10.128.93.13:8888/cam_xiaoN/` |
| Sense A/V 1 | `rtsp://10.128.93.13:8554/cam_sense` | `http://10.128.93.13:8888/cam_sense/` |
| Sense A/V N | `rtsp://10.128.93.13:8554/cam_senseN` | `http://10.128.93.13:8888/cam_senseN/` |

VLC: Open Network → URL → **TCP**; caching ~50–100 ms.

## Record (video + audio)

While MediaMTX is publishing, save a clip with both tracks (Sense A/V) or video-only (`cam_xiao`):

```bash
# Sense (H.264 + AAC) — until Ctrl-C → recordings/rtsp-….mp4
./scripts/record_rtsp.py rtsp://127.0.0.1:8554/cam_sense

# timed + named file
./scripts/record_rtsp.py rtsp://10.128.93.13:8554/cam_sense \
  -o recordings/sense-demo.mp4 -t 120

# video-only path
./scripts/record_rtsp.py rtsp://127.0.0.1:8554/cam_xiao --no-audio
```

Default is **stream-copy** (no re-encode). Use `--reencode` if the player needs H.264/yuv420p. Ctrl-C finishes a playable MP4. See `scripts/record_rtsp.py -h`.

## Config notes

- Base: `mediamtx/mediamtx.yml`; runtime paths: `mediamtx.runtime.yml` (gitignored).
- Encode defaults: Sense A/V CFR ~8 fps, CRF 18, AAC 96 kb/s; **A/V sync** via CFR video PTS + ~300 ms audio delay + soft `aresample=async` + 2 s mux preload (delay OK, smooth play). Override delay with `SENSE_AV_AUDIO_DELAY_MS` (raise if lips behind speech, lower if audio lags).
- Sense publish (`ffmpeg_sense_av.sh`): also tees raw s16le to `udp://127.0.0.1:19055` for Whisper (`SENSE_PCM_UDP_PORT` to override).
- Board defaults (video-only): HVGA 480×320, JPEG q8, 12 fps.
