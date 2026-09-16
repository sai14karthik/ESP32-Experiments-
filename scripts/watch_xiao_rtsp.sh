#!/usr/bin/env bash
# Low-delay RTSP watch (ffplay). Prefers MediaMTX path; falls back to board.
set -euo pipefail
URL="${1:-${XIAO_WATCH_URL:-rtsp://10.128.93.23:8554/cam_xiao}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "watching $URL (TCP, low delay)" >&2
exec ffplay -hide_banner -loglevel warning \
  -rtsp_transport tcp \
  -fflags nobuffer \
  -flags low_delay \
  -framedrop \
  -sync ext \
  "$URL"
