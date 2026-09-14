#!/usr/bin/env bash
# ESP (XIAO CameraWebServerWiFi) → H.264 → MediaMTX path cam_xiao.
#
# Official MediaMTX path for ESP MJPEG:
#   https://github.com/bluenviron/mediamtx/discussions/3575
#   https://mediamtx.org/docs/publish/ffmpeg
#
# Default mode=capture uses a "latest JPEG" file (not a pipe queue).
# Piping curl→ffmpeg was building 10–20s backlog then freezing.
#
# View with WebRTC for lowest lag: http://<mini>:8889/cam_xiao/
# HLS is always several seconds behind by design.
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:${RTSP_PORT:-8554}/${MTX_PATH:-cam_xiao}}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
MODE="${PUBLISH_MODE:-capture}"
FPS="${XIAO_FPS:-5}"
BITRATE="${XIAO_BITRATE:-250k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-35}"

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
BUFSIZE="${XIAO_BUFSIZE:-500k}"

echo "ESP → MediaMTX H.264 (mode=$MODE stall=${STALL_S}s once=${PUBLISH_ONCE:-0})" >&2
echo "  source : $XIAO_URL" >&2
[[ "$MODE" == "capture" ]] && echo "  capture: $CAPTURE_URL (latest-frame, no queue)" >&2
echo "  dest   : $MTX_URL  fps=$FPS bitrate=$BITRATE" >&2
echo "  prefer WebRTC :8889/cam_xiao/  (HLS will always lag more)" >&2

pkill -f "ffmpeg.*${MTX_PATH:-cam_xiao}" 2>/dev/null || true
sleep 0.4

stall_watchdog() {
  local progress="$1" target_pid="$2"
  local last="" last_change=$SECONDS out
  sleep 10
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
      echo "$(date '+%H:%M:%S') stall ${STALL_S}s — restart via MediaMTX" >&2
      kill "$target_pid" 2>/dev/null || true
      sleep 0.2
      kill -9 "$target_pid" 2>/dev/null || true
      return 0
    fi
    sleep 1
  done
}

# Always overwrite one JPEG — ffmpeg re-reads it every frame → no backlog lag.
run_capture_fg() {
  local progress fpid wdog poller jpgdir jpg
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  jpgdir="$(mktemp -d -t xiao_jpg_XXXXXX)"
  jpg="$jpgdir/latest.jpg"

  if ! curl -fsS --max-time 3 -o "$jpg" "$CAPTURE_URL"; then
    echo "WARN: initial capture failed" >&2
    # tiny valid-ish placeholder so ffmpeg can open the input
    printf '\xff\xd8\xff\xd9' >"$jpg"
  fi

  (
    while true; do
      if curl -fsS --max-time 2 -o "$jpgdir/n.jpg" "$CAPTURE_URL" 2>/dev/null; then
        mv -f "$jpgdir/n.jpg" "$jpg"
      fi
      sleep 0.12
    done
  ) &
  poller=$!

  # -loop 1 re-opens latest.jpg each frame (always newest picture).
  ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
    -fflags nobuffer+genpts+discardcorrupt -flags low_delay \
    -f image2 -loop 1 -framerate "$FPS" -i "$jpg" \
    -an \
    -vf "format=yuv420p" \
    -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast \
    -tune zerolatency \
    -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$BUFSIZE" \
    -g "$FPS" -keyint_min "$FPS" -sc_threshold 0 -bf 0 \
    -f rtsp -rtsp_transport tcp "$MTX_URL" &
  fpid=$!

  stall_watchdog "$progress" "$fpid" &
  wdog=$!
  wait "$fpid"
  local rc=$?
  kill "$poller" "$wdog" 2>/dev/null || true
  wait "$poller" "$wdog" 2>/dev/null || true
  rm -rf "$jpgdir"
  rm -f "$progress"
  return "$rc"
}

run_stream_fg() {
  local progress fpid wdog
  progress="$(mktemp -t xiao_str_XXXXXX)"
  ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
    -xerror \
    -fflags nobuffer+genpts+discardcorrupt -flags low_delay \
    -rw_timeout 5000000 \
    -f mjpeg -use_wallclock_as_timestamps 1 -framerate "$FPS" -i "$XIAO_URL" \
    -an \
    -vf "fps=${FPS},format=yuv420p" \
    -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast \
    -tune zerolatency \
    -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize "$BUFSIZE" \
    -g "$FPS" -keyint_min "$FPS" -sc_threshold 0 -bf 0 \
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
    echo "WARN: $CAPTURE_URL not reachable" >&2
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
