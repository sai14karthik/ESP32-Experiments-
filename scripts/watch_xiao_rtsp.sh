#!/usr/bin/env bash
# Watch MediaMTX (or board) RTSP MJPEG with ffplay — smoother motion.
# Default: localhost on the Mini (avoids LabPSK hairpin via .23).
set -euo pipefail
URL="${1:-${XIAO_WATCH_URL:-rtsp://127.0.0.1:8554/cam_xiao}}"
if ! command -v ffplay >/dev/null 2>&1; then
  echo "ffplay not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "watching $URL" >&2
echo "(first few 'start chunk' lines at join are normal for MJPEG RTSP)" >&2
# MJPEG is full-range (yuvj*). Tell swscaler the range so it stops
# "deprecated pixel format / set range correctly".
exec ffplay -hide_banner -loglevel warning \
  -rtsp_transport tcp \
  -fflags discardcorrupt \
  -sync video \
  -framedrop \
  -an \
  -vf "scale=in_range=jpeg:out_range=jpeg" \
  "$URL"
