#!/usr/bin/env bash
# Live Whisper from Sense PCM.
#
# Default source: UDP PCM tee from ffmpeg_sense_av (port 19055) — not MediaMTX RTSP.
# Default model:  turbo  (override with --model).
#
# Setup (Mini):
#   uv sync --group whisper
#   # Terminal 1 — must use current ffmpeg_sense_av (PCM tee):
#   SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh
#   # Terminal 2:
#   ./scripts/sense_whisper_live.sh
#   ./scripts/sense_whisper_live.sh --model large-v3 --vad-db -52
#
# Other sources:
#   ./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio     # MediaMTX off
#   ./scripts/sense_whisper_live.sh --rtsp rtsp://127.0.0.1:8554/cam_sense  # slower
#
# Env: SENSE_PCM_UDP_PORT (default 19055), SENSE_WHISPER_RTSP (unused unless --rtsp)
#
# Docs: firmware/CameraWebServerWiFiSense/README.md  ·  mediamtx/README.md
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

has_src=0
for a in "$@"; do
  case "$a" in
    --pcm-udp|--url|--rtsp|--pcm-udp=*|--url=*|--rtsp=*) has_src=1; break ;;
  esac
done
if [[ $has_src -eq 0 ]]; then
  set -- --pcm-udp "${SENSE_PCM_UDP_PORT:-19055}" "$@"
fi

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
