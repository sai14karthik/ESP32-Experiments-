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
echo "(join may drop a few incomplete MJPEG frames — normal)" >&2
# MJPEG = full-range (legacy yuvj*). Convert to yuv420p with explicit range
# so swscaler does not warn "deprecated pixel format / set range correctly".
# loglevel error hides leftover join noise; video still plays.
exec ffplay -hide_banner -loglevel error \
  -rtsp_transport tcp \
  -fflags discardcorrupt \
  -sync video \
  -framedrop \
  -an \
  -vf "scale=in_range=full:out_range=full,format=yuv420p" \
  "$URL"
