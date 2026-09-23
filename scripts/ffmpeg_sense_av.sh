#!/usr/bin/env bash
# Sense A/V remux for MediaMTX — smooth + A/V synced (delay OK).
# Also tees raw s16le to udp://127.0.0.1:19055 for Whisper (no AAC/RTSP lag).
# Env from MediaMTX: RTSP_PORT, MTX_PATH; optional SENSE_PCM_UDP_PORT (default 19055)
# Args: Sense base URL e.g. http://10.128.93.25
#
# Sync strategy (two HTTP inputs have no shared clock):
# - Regenerate video PTS as perfect CFR (setpts=N/(fps*TB)) so jitter does not drift audio
# - aresample async stretches/squeezes PCM to stay on that timeline (first_pts=0)
# - Modest muxdelay/preload buffers both tracks together (smooth; delay OK)
# - max_interleave_delta=0 is forbidden (stalls); ~2s allows A/V to stay interleaved
# - Whisper must NOT use RTSP — use the PCM UDP tee instead
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"
FPS="${SENSE_AV_FPS:-8}"
PCM_UDP_PORT="${SENSE_PCM_UDP_PORT:-19055}"
PCM_UDP="udp://127.0.0.1:${PCM_UDP_PORT}?pkt_size=960"

exec ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+discardcorrupt \
  -probesize 512k \
  -analyzeduration 500000 \
  -thread_queue_size 2048 \
  -f mjpeg -framerate "$FPS" \
  -i "$VURL" \
  -thread_queue_size 2048 \
  -f s16le -ar 16000 -ac 1 \
  -i "$AURL" \
  -filter_complex \
  "[0:v]fps=${FPS},format=yuv420p,setpts=N/(${FPS}*TB)[v];\
   [1:a]aresample=16000:async=1000:first_pts=0,highpass=f=80,volume=0.8[a]" \
  -map "[v]" -map "[a]" \
  -fps_mode cfr \
  -r "$FPS" \
  -c:v libx264 \
  -preset veryfast \
  -profile:v high \
  -pix_fmt yuv420p \
  -bf 0 \
  -g $((FPS * 2)) \
  -keyint_min $((FPS * 2)) \
  -crf 18 \
  -maxrate 8000k \
  -bufsize 16000k \
  -x264-params "scenecut=0:repeat-headers=1" \
  -c:a aac \
  -b:a 128k \
  -ar 16000 \
  -ac 1 \
  -max_interleave_delta 2000000 \
  -muxdelay 1.5 \
  -muxpreload 1.5 \
  -f rtsp \
  -rtsp_transport tcp \
  "$OUT" \
  -map 1:a:0 \
  -c:a pcm_s16le \
  -ac 1 \
  -ar 16000 \
  -f s16le \
  "$PCM_UDP"
