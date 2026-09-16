#!/usr/bin/env bash
# Low-latency watch of MediaMTX cam_xiao (H.264).
set -euo pipefail
URL="${1:-${XIAO_WATCH_URL:-rtsp://127.0.0.1:8554/cam_xiao}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "watching $URL (low delay)" >&2
exec ffplay -hide_banner -loglevel warning \
  -rtsp_transport tcp \
  -fflags nobuffer+discardcorrupt \
  -flags low_delay \
  -framedrop \
  -sync ext \
  -probesize 32 \
  -analyzeduration 0 \
  -an \
  "$URL"
