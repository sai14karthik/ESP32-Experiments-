#!/usr/bin/env bash
# Live Whisper while Sense A/V is streaming (preferred) or from raw /audio.
# Default model: small.en (override with --model tiny.en / base.en / …).
#
# With MediaMTX cam_sense (VLC + recognition together) — default:
#   ./scripts/sense_whisper_live.sh
#   # on Mini: rtsp://127.0.0.1:8554/cam_sense  (override with SENSE_WHISPER_RTSP)
#
# Direct Sense /audio (only if MediaMTX is NOT using /audio):
#   ./scripts/sense_whisper_live.sh --url http://10.128.93.25/audio
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Default to MediaMTX RTSP unless caller already passed --rtsp or --url.
# Prefer loopback on the Mini (same host as MediaMTX); LAN IP for remote clients.
has_src=0
for a in "$@"; do
  case "$a" in
    --rtsp|--url|--rtsp=*|--url=*) has_src=1; break ;;
  esac
done
if [[ $has_src -eq 0 ]]; then
  set -- --rtsp "${SENSE_WHISPER_RTSP:-rtsp://127.0.0.1:8554/cam_sense}" "$@"
fi

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
