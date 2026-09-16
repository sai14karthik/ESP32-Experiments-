#!/usr/bin/env bash
# Bridge ESP camera → MediaMTX path cam_xiao.
#
# Canonical pattern (MediaMTX official webcam docs, adapted for ESP MJPEG):
#   https://mediamtx.org/docs/publish/generic-webcams
#   Espressif: ESP32-S3 has no HW H.264 — host must transcode MJPEG.
#
# Prefer:
#   XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
# Watch live: http://<MINI_IP>:8889/cam_xiao/
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:${RTSP_PORT:-8554}/${MTX_PATH:-cam_xiao}}"
XIAO_URL="${1:-${XIAO_RTSP_URL:-${XIAO_MJPEG_URL:-}}}"
if [[ -z "${PUBLISH_MODE:-}" ]]; then
  if [[ "${XIAO_URL}" == rtsp://* ]]; then
    MODE=rtsp
  elif [[ "${XIAO_URL}" == http://* ]]; then
    MODE=stream
  else
    MODE=capture
  fi
else
  MODE="${PUBLISH_MODE}"
fi

FPS="${XIAO_FPS:-10}"
BITRATE="${XIAO_BITRATE:-1200k}"
RETRY_S="${PUBLISH_RETRY_S:-3}"
STALL_S="${PUBLISH_STALL_S:-45}"
# Wait this long for the first encoded frame before declaring stall.
CONNECT_S="${PUBLISH_CONNECT_S:-90}"
HOLD_LAST="${PUBLISH_HOLD_LAST:-0}"
CURL_MAX_S="${PUBLISH_CURL_MAX_S:-8}"
MAX_LIFE_S="${PUBLISH_MAX_LIFE_S:-0}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi

if [[ -z "$XIAO_URL" ]]; then
  cat >&2 <<'EOF'
Usage:
  ./scripts/publish_xiao.sh rtsp://10.128.93.25:554/mjpeg/1
Prefer: XIAO_RTSP_URL=rtsp://… ./scripts/mediamtx_run.sh
EOF
  exit 2
fi

capture_url_from() {
  local u="$1"
  u="${u%%/stream}"
  u="${u%:81}"
  u="${u%/}"
  echo "${u}/capture"
}

CAPTURE_URL=""
if [[ "$MODE" == "capture" || "$MODE" == "stream" ]]; then
  CAPTURE_URL="$(capture_url_from "$XIAO_URL")"
fi

echo "Bridging XIAO → MediaMTX (mode=$MODE connect=${CONNECT_S}s stall=${STALL_S}s once=${PUBLISH_ONCE:-0})" >&2
echo "  source : $XIAO_URL" >&2
echo "  dest   : $MTX_URL  fps=$FPS bitrate=$BITRATE" >&2

kill_pgid() {
  local pid="${1:-}"
  [[ -z "$pid" ]] && return 0
  kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  sleep 0.3
  kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

watch_progress() {
  local progress="$1" fpid="$2" started="$3"
  local last="" last_change=$SECONDS out now got_frame=0
  while kill -0 "$fpid" 2>/dev/null; do
    if [[ -f "$progress" ]]; then
      out="$(grep -E '^out_time_ms=' "$progress" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
      if [[ -n "$out" && "$out" != "$last" ]]; then
        last="$out"
        last_change=$SECONDS
        if [[ "$got_frame" -eq 0 ]]; then
          got_frame=1
          echo "$(date '+%H:%M:%S') first frame OK" >&2
        fi
      fi
    fi
    now=$SECONDS
    if [[ "$got_frame" -eq 0 ]]; then
      if (( now - started >= CONNECT_S )); then
        echo "$(date '+%H:%M:%S') no frames in ${CONNECT_S}s — restart" >&2
        return 1
      fi
    elif (( STALL_S > 0 && now - last_change >= STALL_S )); then
      echo "$(date '+%H:%M:%S') stall ${STALL_S}s — restart" >&2
      return 1
    fi
    if (( MAX_LIFE_S > 0 && now - started >= MAX_LIFE_S )); then
      echo "$(date '+%H:%M:%S') max life ${MAX_LIFE_S}s — refresh" >&2
      return 1
    fi
    sleep 1
  done
  return 0
}

# MediaMTX webcam recipe + ESP MJPEG.
# Micro-RTSP timestamps are broken → without rate-limit, ffmpeg encodes at
# "90000 fps" (MB rate limit / frames duplicated / WebRTC discards thousands).
# Fix: stamp frames with wall clock, then fps=N keeps real-time 10 fps.
ffmpeg_h264_out() {
  local prog="$1"
  shift
  ffmpeg -hide_banner -loglevel error \
    -nostats \
    -progress "$prog" \
    "$@" \
    -an \
    -vf "fps=${FPS},format=yuv420p" \
    -c:v libx264 \
    -preset ultrafast \
    -tune zerolatency \
    -profile:v baseline \
    -level 3.1 \
    -b:v "$BITRATE" \
    -g $((FPS * 2)) \
    -keyint_min "$FPS" \
    -bf 0 \
    -x264-params "slice-max-size=1000:scenecut=0:repeat-headers=1" \
    -flush_packets 1 \
    -muxdelay 0 \
    -muxpreload 0 \
    -f rtsp \
    -rtsp_transport tcp \
    "$MTX_URL"
}

run_rtsp() {
  local progress fpid started
  progress="$(mktemp -t xiao_rtsp_XXXXXX)"
  started=$SECONDS
  trap "rm -f '$progress'; kill_pgid \"\${fpid:-}\"" RETURN
  set -m
  ffmpeg_h264_out "$progress" \
    -fflags +genpts+discardcorrupt \
    -use_wallclock_as_timestamps 1 \
    -rtsp_transport tcp \
    -i "$XIAO_URL" &
  fpid=$!
  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pgid "$fpid"
    return 1
  fi
  wait "$fpid" 2>/dev/null
  return $?
}

run_stream() {
  local progress fpid started
  progress="$(mktemp -t xiao_str_XXXXXX)"
  started=$SECONDS
  trap "rm -f '$progress'; kill_pgid \"\${fpid:-}\"" RETURN
  set -m
  ffmpeg_h264_out "$progress" \
    -fflags +genpts+discardcorrupt \
    -f mjpeg \
    -i "$XIAO_URL" &
  fpid=$!
  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pgid "$fpid"
    return 1
  fi
  wait "$fpid" 2>/dev/null || true
}

run_capture() {
  local progress fpid started jpgdir lastjpg
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  jpgdir="$(mktemp -d -t xiao_hold_XXXXXX)"
  lastjpg="$jpgdir/last.jpg"
  started=$SECONDS
  trap "rm -rf '$jpgdir'; rm -f '$progress'; kill_pgid \"\${fpid:-}\"" RETURN
  set -m
  (
    interval="$(awk -v f="$FPS" 'BEGIN{printf "%.3f", 1/f}')"
    while true; do
      t0="$(python3 -c 'import time; print(time.time())')"
      got=0
      if curl -fsS --max-time "$CURL_MAX_S" -o "$jpgdir/n.jpg" "$CAPTURE_URL" 2>/dev/null; then
        mv -f "$jpgdir/n.jpg" "$lastjpg"
        got=1
      fi
      if [[ "$got" -eq 1 || ( "$HOLD_LAST" == "1" && -f "$lastjpg" ) ]]; then
        [[ -f "$lastjpg" ]] && cat "$lastjpg"
      fi
      python3 -c "import time,sys; t0=float(sys.argv[1]); i=float(sys.argv[2]); d=i-(time.time()-t0); time.sleep(d if d>0 else 0)" "$t0" "$interval"
    done
  ) | ffmpeg_h264_out "$progress" \
      -fflags +genpts+discardcorrupt \
      -f image2pipe \
      -framerate "$FPS" \
      -c:v mjpeg \
      -i - &
  fpid=$!
  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pgid "$fpid"
    return 1
  fi
  wait "$fpid" 2>/dev/null || true
}

run_once() {
  case "$MODE" in
    rtsp) run_rtsp ;;
    stream) run_stream ;;
    *) run_capture ;;
  esac
}

if [[ "$MODE" == "capture" ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found" >&2
    exit 1
  fi
  if curl -fsS --max-time "$CURL_MAX_S" -o /dev/null "$CAPTURE_URL"; then
    echo "capture OK: $CAPTURE_URL" >&2
  else
    echo "WARN: $CAPTURE_URL not reachable — will keep trying" >&2
  fi
elif [[ "$MODE" == "rtsp" ]]; then
  echo "RTSP pull (wallclock + fps=${FPS} — stops Micro-RTSP frame flood)" >&2
fi

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  ec=$?
  # Longer pause so ESP Micro-RTSP can drop the old TCP session before restart.
  sleep 5
  exit "$ec"
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') bridge restart in ${RETRY_S}s…" >&2
  sleep "$RETRY_S"
done
