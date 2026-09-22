#!/usr/bin/env bash
# Live Whisper from Sense PCM (smooth capture + VAD + tiny.en worker).
#
#   ./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio
#   ./scripts/sense_whisper_live.sh --rtsp rtsp://10.128.93.23:8554/cam_sense
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
