#!/usr/bin/env bash
# Sense A/V remux for MediaMTX — prioritize SMOOTH playback (delay OK).
# Env from MediaMTX: RTSP_PORT, MTX_PATH
# Args: Sense base URL e.g. http://10.128.93.25
#
# Research notes (ESP dual-stream + ffmpeg mux):
# - max_interleave_delta=0 means "wait forever for every stream" → stuck/stutter
# - nobuffer/low_delay/flush_packets fight smoothness when ESP FPS jitters
# - Prefer CFR + larger queues + modest mux preload (user accepts delay)
# - Fixed ~10 fps on ESP + QVGA is the stable band for Wi‑Fi dual HTTP
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"

exec ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+discardcorrupt \
  -probesize 512k \
  -analyzeduration 500000 \
  -thread_queue_size 1024 \
  -f mjpeg -framerate 10 \
  -i "$VURL" \
  -thread_queue_size 1024 \
  -f s16le -ar 16000 -ac 1 \
  -i "$AURL" \
  -map 0:v:0 -map 1:a:0 \
  -vf "fps=10,format=yuv420p" \
  -af "aresample=async=1000:first_pts=0,highpass=f=80,volume=0.8" \
  -fps_mode cfr \
  -r 10 \
  -c:v libx264 \
  -preset veryfast \
  -profile:v baseline \
  -pix_fmt yuv420p \
  -bf 0 \
  -g 20 \
  -keyint_min 20 \
  -crf 23 \
  -maxrate 2000k \
  -bufsize 4000k \
  -x264-params "scenecut=0:repeat-headers=1" \
  -c:a aac \
  -b:a 64k \
  -ar 16000 \
  -ac 1 \
  -max_interleave_delta 1000000 \
  -muxdelay 0.5 \
  -muxpreload 0.5 \
  -f rtsp \
  -rtsp_transport tcp \
  "$OUT"
