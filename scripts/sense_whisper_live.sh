#!/usr/bin/env bash
# Live Whisper while Sense A/V is streaming (preferred) or from raw /audio.
#
# With MediaMTX cam_sense (VLC + recognition together) — default:
#   ./scripts/sense_whisper_live.sh
#   ./scripts/sense_whisper_live.sh --rtsp rtsp://10.128.93.23:8554/cam_sense
#
# Direct Sense /audio (only if MediaMTX is NOT using /audio):
#   ./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -eq 0 ]]; then
  set -- --rtsp "${SENSE_WHISPER_RTSP:-rtsp://10.128.93.23:8554/cam_sense}"
fi

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
