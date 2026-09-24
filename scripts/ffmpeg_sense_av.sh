#!/usr/bin/env bash
# Sense A/V remux for MediaMTX — high quality + A/V synced (delay OK).
# Tees speech-processed s16le to udp://127.0.0.1:19055(+n) for Whisper.
# Env from MediaMTX: RTSP_PORT, MTX_PATH; optional SENSE_PCM_UDP_PORT, SENSE_AV_FPS,
#      SENSE_AV_AUDIO_DELAY_MS (default 300 — delay audio vs video for lip sync)
# Args: Sense base URL e.g. http://10.128.93.15
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"
FPS="${SENSE_AV_FPS:-8}"
# Seconds for -itsoffset on audio (video path usually lags PCM)
AUDIO_DELAY_MS="${SENSE_AV_AUDIO_DELAY_MS:-300}"
AUDIO_DELAY_S="$(awk "BEGIN { printf \"%.3f\", ${AUDIO_DELAY_MS}/1000 }")"

# PCM UDP port is tied to board IP (stable) — not MediaMTX path order.
if [[ -z "${SENSE_PCM_UDP_PORT:-}" ]]; then
  _host="${BASE#http://}"
  _host="${_host#https://}"
  _host="${_host%%/*}"
  _host="${_host%%:*}"
  _octet="${_host##*.}"
  case "${_octet}" in
    25) PCM_UDP_PORT=19055 ;;  # wall
    34) PCM_UDP_PORT=19056 ;;  # collar (legacy DHCP)
    15) PCM_UDP_PORT=19056 ;;  # collar (current DHCP)
    *)  PCM_UDP_PORT=$((19050 + (${_octet} % 10))) ;;
  esac
else
  PCM_UDP_PORT="${SENSE_PCM_UDP_PORT}"
fi
PCM_UDP="udp://127.0.0.1:${PCM_UDP_PORT}?pkt_size=960"

LOG="${SENSE_AV_LOG:-/tmp/ffmpeg_sense_av.${MTX_PATH}.log}"
{
  echo "$(date '+%F %T') start path=${MTX_PATH} base=${BASE} out=${OUT} pcm=${PCM_UDP} fps=${FPS} audio_delay_s=${AUDIO_DELAY_S}"
  echo "  video=${VURL}"
  echo "  audio=${AURL}"
} >>"$LOG"

# Known-good filter chain (no adelay/fifo — those broke Mini ffmpeg).
# Audio delayed via -itsoffset so lips stay closer without fragile filters.
exec ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+discardcorrupt \
  -reconnect 1 \
  -reconnect_streamed 1 \
  -reconnect_delay_max 5 \
  -rw_timeout 15000000 \
  -probesize 512k \
  -analyzeduration 500000 \
  -thread_queue_size 2048 \
  -f mjpeg -framerate "$FPS" \
  -i "$VURL" \
  -itsoffset "$AUDIO_DELAY_S" \
  -reconnect 1 \
  -reconnect_streamed 1 \
  -reconnect_delay_max 5 \
  -rw_timeout 15000000 \
  -thread_queue_size 2048 \
  -f s16le -ar 16000 -ac 1 \
  -i "$AURL" \
  -filter_complex \
  "[0:v]fps=${FPS},format=yuv420p,setpts=N/(${FPS}*TB)[v];\
   [1:a]asplit=2[a0][a1];\
   [a0]highpass=f=80,lowpass=f=7500,acompressor=threshold=-28dB:ratio=3:attack=15:release=150:makeup=2,alimiter=limit=0.89,aresample=16000:async=1000:first_pts=0[a];\
   [a1]highpass=f=100,lowpass=f=7000,equalizer=f=1200:t=q:w=1.2:g=2,acompressor=threshold=-30dB:ratio=3:attack=10:release=120:makeup=3,alimiter=limit=0.89[a_pcm]" \
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
  -maxrate 6000k \
  -bufsize 12000k \
  -x264-params "scenecut=0:repeat-headers=1" \
  -c:a aac \
  -b:a 96k \
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
