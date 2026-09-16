#!/usr/bin/env bash
# Watch MediaMTX (or board) RTSP MJPEG with ffplay.
set -euo pipefail
URL="${1:-${XIAO_WATCH_URL:-rtsp://127.0.0.1:8554/cam_xiao}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "watching $URL" >&2
exec ffplay -rtsp_transport tcp "$URL"
