#!/usr/bin/env bash
# ESP (XIAO CameraWebServerWiFi) → H.264 → MediaMTX path cam_xiao.
#
# Why this exists (official MediaMTX guidance):
#   ESP MJPEG over HTTP is not a native MediaMTX input.
#   FFmpeg must publish into MediaMTX (runOnInit). See:
#     https://github.com/bluenviron/mediamtx/discussions/3575
#     https://mediamtx.org/docs/publish/ffmpeg
#
# Why H.264 (not -c copy):
#   HLS/WebRTC require H.264/AV1/… — not MJPEG.
#     https://mediamtx.org/docs/read/hls
#
# Why capture mode (default) instead of :81/stream:
#   Long-lived ESP MJPEG TCP often stalls ~1 min on Wi‑Fi.
#   Polling GET /capture is more reliable; MediaMTX runOnInitRestart
#   restarts this script if ffmpeg exits (hooks docs).
#
# Usage:
#   PUBLISH_ONCE=1 ./scripts/publish_xiao.sh http://10.128.93.25:81/stream
#   PUBLISH_MODE=stream ./scripts/publish_xiao.sh http://10.128.93.25:81/stream
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:${RTSP_PORT:-8554}/${MTX_PATH:-cam_xiao}}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
MODE="${PUBLISH_MODE:-capture}"
FPS="${XIAO_FPS:-6}"
BITRATE="${XIAO_BITRATE:-400k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-25}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found — brew install ffmpeg" >&2
  exit 1
fi
if [[ -z "$XIAO_URL" ]]; then
  echo "Usage: $0 http://<xiao-ip>:81/stream" >&2
  exit 2
fi
if [[ "$MODE" == "capture" ]] && ! command -v curl >/dev/null 2>&1; then
  echo "curl not found" >&2
  exit 1
fi

capture_url_from() {
  local u="$1"
  u="${u%%/stream}"
  u="${u%:81}"
  u="${u%/}"
  echo "${u}/capture"
}
CAPTURE_URL="$(capture_url_from "$XIAO_URL")"

echo "ESP → MediaMTX H.264 bridge (mode=$MODE stall=${STALL_S}s once=${PUBLISH_ONCE:-0})" >&2
echo "  source : $XIAO_URL" >&2
[[ "$MODE" == "capture" ]] && echo "  capture: $CAPTURE_URL" >&2
echo "  dest   : $MTX_URL  fps=$FPS bitrate=$BITRATE" >&2

# Avoid double-publisher fights ("closing existing publisher").
pkill -f "ffmpeg.*${MTX_PATH:-cam_xiao}" 2>/dev/null || true
sleep 0.4

stall_watchdog() {
  local progress="$1" target_pid="$2"
  local last="" last_change=$SECONDS out
  sleep 8
  last_change=$SECONDS
  while kill -0 "$target_pid" 2>/dev/null; do
    if [[ -f "$progress" ]]; then
      out="$(grep -E '^out_time_ms=' "$progress" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
      if [[ -n "$out" && "$out" != "$last" ]]; then
        last="$out"
        last_change=$SECONDS
      fi
    fi
    if (( SECONDS - last_change >= STALL_S )); then
      echo "$(date '+%H:%M:%S') stall ${STALL_S}s — exit so MediaMTX runOnInitRestart can relaunch" >&2
      kill "$target_pid" 2>/dev/null || true
      sleep 0.3
      kill -9 "$target_pid" 2>/dev/null || true
      return 0
    fi
    sleep 1
  done
}

run_capture_fg() {
  local progress fpid wdog
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  (
    while true; do
      curl -fsS --max-time 3 "$CAPTURE_URL" 2>/dev/null || sleep 0.5
      sleep "$(awk -v f="$FPS" 'BEGIN{printf "%.3f", 1/f}')"
    done
  ) | ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
      -fflags +genpts+discardcorrupt \
      -f image2pipe -framerate "$FPS" -c:v mjpeg -i - \
      -an -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast \
      -tune zerolatency -pix_fmt yuv420p \
      -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$BITRATE" \
      -g $((FPS * 2)) -keyint_min "$FPS" -sc_threshold 0 -bf 0 \
      -f rtsp -rtsp_transport tcp "$MTX_URL" &
  fpid=$!
  stall_watchdog "$progress" "$fpid" &
  wdog=$!
  wait "$fpid"
  local rc=$?
  kill "$wdog" 2>/dev/null || true
  wait "$wdog" 2>/dev/null || true
  rm -f "$progress"
  return "$rc"
}

run_stream_fg() {
  local progress fpid wdog
  progress="$(mktemp -t xiao_str_XXXXXX)"
  # Official-style pull of :81/stream, but re-encode H.264 for browsers.
  # -xerror + rw_timeout: exit on EOF/stall so runOnInitRestart can recover.
  ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
    -xerror \
    -fflags +nobuffer+genpts+discardcorrupt -flags low_delay \
    -rw_timeout 8000000 \
    -f mjpeg -use_wallclock_as_timestamps 1 -r "$FPS" -i "$XIAO_URL" \
    -an -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast \
    -tune zerolatency -pix_fmt yuv420p \
    -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$BITRATE" \
    -g $((FPS * 2)) -keyint_min "$FPS" -sc_threshold 0 -bf 0 \
    -f rtsp -rtsp_transport tcp "$MTX_URL" &
  fpid=$!
  stall_watchdog "$progress" "$fpid" &
  wdog=$!
  wait "$fpid"
  local rc=$?
  kill "$wdog" 2>/dev/null || true
  wait "$wdog" 2>/dev/null || true
  rm -f "$progress"
  return "$rc"
}

run_once() {
  if [[ "$MODE" == "stream" ]]; then
    run_stream_fg
  else
    run_capture_fg
  fi
}

if [[ "$MODE" == "capture" ]]; then
  if curl -fsS --max-time 3 -o /dev/null "$CAPTURE_URL"; then
    echo "capture OK: $CAPTURE_URL" >&2
  else
    echo "WARN: $CAPTURE_URL not reachable (is CameraWebServerWiFi up?)" >&2
  fi
fi

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  exit $?
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') bridge restart in ${RETRY_S}s…" >&2
  sleep "$RETRY_S"
done
