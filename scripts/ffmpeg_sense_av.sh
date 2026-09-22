#!/usr/bin/env bash
# Sense A/V remux for MediaMTX (called from mediamtx_run.sh runOnInit).
# Env from MediaMTX: RTSP_PORT, MTX_PATH
# Args: Sense base URL e.g. http://10.128.93.25
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"

# No wallclock on MJPEG — with dual HTTP the ESP delivers uneven FPS; wallclock
# timestamps grow latency (stuck/lagged). Reclock with setpts instead.
# Drop late frames; never hold video for audio (max_interleave_delta 0).
exec ffmpeg -hide_banner -loglevel warning \
  -fflags nobuffer+genpts+discardcorrupt \
  -flags low_delay \
  -avioflags direct \
  -probesize 32 \
  -analyzeduration 0 \
  -thread_queue_size 64 \
  -f mjpeg -framerate 12 \
  -i "$VURL" \
  -thread_queue_size 256 \
  -f s16le -ar 16000 -ac 1 \
  -i "$AURL" \
  -map 0:v:0 -map 1:a:0 \
  -vf "setpts=N/(12*TB),format=yuv420p" \
  -af "highpass=f=80,aresample=async=1:first_pts=0,volume=0.8" \
  -c:v libx264 \
  -preset ultrafast \
  -tune zerolatency \
  -profile:v baseline \
  -pix_fmt yuv420p \
  -bf 0 \
  -g 12 \
  -keyint_min 12 \
  -crf 23 \
  -maxrate 1500k \
  -bufsize 750k \
  -x264-params "scenecut=0:repeat-headers=1" \
  -c:a aac \
  -b:a 48k \
  -ar 16000 \
  -ac 1 \
  -max_interleave_delta 0 \
  -flush_packets 1 \
  -muxdelay 0 \
  -muxpreload 0 \
  -f rtsp \
  -rtsp_transport tcp \
  "$OUT"
