#!/usr/bin/env bash
# Live Whisper from Sense PCM — Apple Metal via mlx-whisper (default).
#
# Default source: UDP PCM tee :19055 (no MediaMTX lag)
# Default model:  turbo  (mlx-community/whisper-large-v3-turbo)
#
# Setup (Mini):
#   uv sync --group whisper
#   SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh   # terminal 1
#   ./scripts/sense_whisper_live.sh                               # terminal 2
#
# Expect: [backend] mlx / Metal , [diarize] on , [YOU] / [OTHER_1] labels
# Overrides:
#   ./scripts/sense_whisper_live.sh --vad-db -52
#   ./scripts/sense_whisper_live.sh --enroll-you ~/myvoice.wav
#   ./scripts/sense_whisper_live.sh --no-diarize
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
