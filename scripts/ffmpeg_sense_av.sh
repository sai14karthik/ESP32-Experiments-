#!/usr/bin/env bash
# Sense A/V remux for MediaMTX — smooth + A/V synced (delay OK).
# Also tees speech-processed s16le to udp://127.0.0.1:19055 for Whisper.
# Env from MediaMTX: RTSP_PORT, MTX_PATH; optional SENSE_PCM_UDP_PORT (default 19055)
# Args: Sense base URL e.g. http://10.128.93.25
#
# Sync strategy (two HTTP inputs have no shared clock):
# - Regenerated video PTS + aresample async for A/V lock
# - asplit *raw* first, then speech chain on each branch (filter-then-asplit
#   starves the PCM tee and can block RTSP publish)
# - Whisper uses the PCM UDP tee (not RTSP AAC)
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"
FPS="${SENSE_AV_FPS:-8}"
PCM_UDP_PORT="${SENSE_PCM_UDP_PORT:-19055}"
PCM_UDP="udp://127.0.0.1:${PCM_UDP_PORT}?pkt_size=960"

# MediaMTX hides runOnInit stderr — log file for debugging on Mini.
LOG="${SENSE_AV_LOG:-/tmp/ffmpeg_sense_av.${MTX_PATH}.log}"
echo "$(date '+%F %T') start path=${MTX_PATH} base=${BASE} out=${OUT} pcm=${PCM_UDP}" >>"$LOG"

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
   [1:a]asplit=2[a0][a1];\
   [a0]highpass=f=80,lowpass=f=7500,acompressor=threshold=-28dB:ratio=3:attack=15:release=150:makeup=3,alimiter=limit=0.9,aresample=16000:async=1000:first_pts=0[a];\
   [a1]highpass=f=80,lowpass=f=7500,acompressor=threshold=-28dB:ratio=3:attack=15:release=150:makeup=3,alimiter=limit=0.9[a_pcm]" \
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
  -b:a 64k \
  -ar 16000 \
  -ac 1 \
  -max_interleave_delta 2000000 \
  -muxdelay 1.5 \
  -muxpreload 1.5 \
  -f rtsp \
  -rtsp_transport tcp \
  "$OUT" \
  -map "[a_pcm]" \
  -c:a pcm_s16le \
  -ac 1 \
  -ar 16000 \
  -f s16le \
  "$PCM_UDP" \
  >>"$LOG" 2>&1
