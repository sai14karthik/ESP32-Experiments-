#!/usr/bin/env bash
# Watch MediaMTX (or board) RTSP MJPEG with ffplay.
# Default: localhost on the Mini (avoids LabPSK hairpin via .23).
set -euo pipefail
URL="${1:-${XIAO_WATCH_URL:-rtsp://127.0.0.1:8554/cam_xiao}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "watching $URL" >&2
echo "(first few 'start chunk' lines at join are normal for MJPEG RTSP)" >&2
# MJPEG over RTSP: do NOT use -fflags nobuffer (drops more start chunks).
exec ffplay -hide_banner -loglevel warning \
  -rtsp_transport tcp \
  -fflags discardcorrupt \
  -flags low_delay \
  -framedrop \
  -an \
  "$URL"
