#!/usr/bin/env bash
# Live Whisper from Sense PCM — default: large-v3 + local UDP tee (no MediaMTX/AAC lag).
#
# Terminal 1 (restart after sync so tee is active):
#   SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh
# Terminal 2:
#   ./scripts/sense_whisper_live.sh
#   # → --pcm-udp 19055 --model large-v3
#
# Overrides:
#   ./scripts/sense_whisper_live.sh --model medium.en
#   ./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio   # if MediaMTX off
#   ./scripts/sense_whisper_live.sh --rtsp rtsp://127.0.0.1:8554/cam_sense  # slower
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
