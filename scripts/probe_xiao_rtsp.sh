#!/usr/bin/env bash
# Probe ESP or MediaMTX RTSP (TCP).
set -euo pipefail
URL="${1:-${XIAO_RTSP_URL:-rtsp://10.128.93.25:554/mjpeg/1}}"
if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ffprobe not found. Install: brew install ffmpeg" >&2
  exit 1
fi
echo "probe $URL" >&2
ffprobe -hide_banner -rtsp_transport tcp -select_streams v:0 \
  -show_entries stream=codec_name,width,height,avg_frame_rate \
  -of default=nokey=0:noprint_wrappers=1 \
  "$URL"
