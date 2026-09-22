#!/usr/bin/env bash
# Sense A/V remux for MediaMTX — smooth + lip-sync (overall delay OK).
# Env from MediaMTX: RTSP_PORT, MTX_PATH
# Args: Sense base URL e.g. http://10.128.93.25
#
# Sync strategy:
# - Wallclock PTS on BOTH inputs so A and V share real time
# - Do NOT reclock video with setpts=N/(fps*TB) (that desyncs lips)
# - Mild aresample=async keeps audio continuous without drifting far
# - JPEG+HTTP is slower than PCM → delay audio a bit (SENSE_AV_AUDIO_DELAY_MS)
# - Keep mux buffers for smoothness; max_interleave_delta must NOT be 0
set -euo pipefail

BASE="${1:?need http://esp-ip}"
BASE="${BASE%/}"
VURL="${BASE}:81/stream"
AURL="${BASE}/audio"
OUT="rtsp://127.0.0.1:${RTSP_PORT:?}/${MTX_PATH:?}"

# Audio leads video by ~JPEG encode + HTTP frame time; delay audio to match lips.
DELAY_MS="${SENSE_AV_AUDIO_DELAY_MS:-180}"
DELAY_SEC="$(awk -v ms="$DELAY_MS" 'BEGIN { printf "%.3f", ms/1000.0 }')"

exec ffmpeg -hide_banner -loglevel warning \
  -fflags +genpts+discardcorrupt \
  -probesize 512k \
  -analyzeduration 500000 \
  -thread_queue_size 1024 \
  -f mjpeg -framerate 10 \
  -use_wallclock_as_timestamps 1 \
  -i "$VURL" \
  -thread_queue_size 1024 \
  -itsoffset "$DELAY_SEC" \
  -f s16le -ar 16000 -ac 1 \
  -use_wallclock_as_timestamps 1 \
  -i "$AURL" \
  -map 0:v:0 -map 1:a:0 \
  -vf "fps=10,setpts=PTS-STARTPTS,format=yuv420p" \
  -af "asetpts=PTS-STARTPTS,aresample=async=1:min_hard_comp=0.100:first_pts=0,highpass=f=80,volume=0.8" \
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
  -max_interleave_delta 500000 \
  -muxdelay 0.4 \
  -muxpreload 0.4 \
  -f rtsp \
  -rtsp_transport tcp \
  "$OUT"
