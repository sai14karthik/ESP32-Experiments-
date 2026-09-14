#!/usr/bin/env bash
# ESP CameraWebServerWiFi → H.264 → MediaMTX (cam_xiao).
#
# Design (from MediaMTX docs + lab failure modes):
# 1) ESP MJPEG is not a MediaMTX source → FFmpeg must publish
#    https://github.com/bluenviron/mediamtx/discussions/3575
# 2) Browsers need H.264 baseline, no B-frames (WebRTC)
#    https://mediamtx.org/docs/features/webrtc-specific-features
# 3) Pipe queues caused 20s lag → "latest JPEG" file, always newest frame
# 4) On Mac Mini use VideoToolbox when available (smoother than libx264)
#
# PUBLISH_MODE=capture (default) | stream
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:${RTSP_PORT:-8554}/${MTX_PATH:-cam_xiao}}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
MODE="${PUBLISH_MODE:-capture}"
FPS="${XIAO_FPS:-4}"
BITRATE="${XIAO_BITRATE:-350k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-45}"

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

# Prefer Apple encoder on Mini (less CPU stutter).
ENC=( -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast -tune zerolatency )
if ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox; then
  ENC=( -c:v h264_videotoolbox -profile:v baseline -b:v "$BITRATE" -realtime 1 -bf 0 )
  echo "encoder: h264_videotoolbox" >&2
else
  ENC=( -c:v libx264 -profile:v baseline -level 3.0 -preset ultrafast -tune zerolatency
        -b:v "$BITRATE" -maxrate "$BITRATE" -bufsize 700k
        -g "$FPS" -keyint_min "$FPS" -sc_threshold 0 -bf 0 )
  echo "encoder: libx264" >&2
fi

echo "ESP → MediaMTX (mode=$MODE fps=$FPS once=${PUBLISH_ONCE:-0})" >&2
echo "  in : $XIAO_URL" >&2
[[ "$MODE" == "capture" ]] && echo "  cap: $CAPTURE_URL" >&2
echo "  out: $MTX_URL" >&2

pkill -f "ffmpeg.*${MTX_PATH:-cam_xiao}" 2>/dev/null || true
sleep 0.3

stall_watchdog() {
  local progress="$1" target_pid="$2"
  local last="" last_change=$SECONDS out
  sleep 12
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
      echo "$(date '+%H:%M:%S') stall — restart" >&2
      kill "$target_pid" 2>/dev/null || true
      sleep 0.2
      kill -9 "$target_pid" 2>/dev/null || true
      return 0
    fi
    sleep 1
  done
}

run_capture_fg() {
  local progress fpid wdog poller jpgdir jpg interval
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  jpgdir="$(mktemp -d -t xiao_jpg_XXXXXX)"
  jpg="$jpgdir/latest.jpg"
  interval="$(awk -v f="$FPS" 'BEGIN{printf "%.3f", 1/f}')"

  curl -fsS --max-time 3 -o "$jpg" "$CAPTURE_URL" 2>/dev/null || printf '\xff\xd8\xff\xd9' >"$jpg"

  (
    while true; do
      if curl -fsS --max-time 2 -o "$jpgdir/n.jpg" "$CAPTURE_URL" 2>/dev/null; then
        mv -f "$jpgdir/n.jpg" "$jpg"
      fi
      sleep "$interval"
    done
  ) &
  poller=$!

  ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
    -fflags nobuffer+genpts+discardcorrupt -flags low_delay \
    -f image2 -loop 1 -framerate "$FPS" -i "$jpg" \
    -an -vf "format=yuv420p" \
    "${ENC[@]}" \
    -f rtsp -rtsp_transport tcp "$MTX_URL" &
  fpid=$!

  stall_watchdog "$progress" "$fpid" &
  wdog=$!
  wait "$fpid"
  local rc=$?
  kill "$poller" "$wdog" 2>/dev/null || true
  wait "$poller" "$wdog" 2>/dev/null || true
  rm -rf "$jpgdir" "$progress"
  return "$rc"
}

run_stream_fg() {
  local progress fpid wdog
  progress="$(mktemp -t xiao_str_XXXXXX)"
  # HTTP reconnect helps when :81/stream drops (ffmpeg http options).
  ffmpeg -hide_banner -loglevel error -nostats -progress "$progress" \
    -xerror \
    -fflags nobuffer+genpts+discardcorrupt -flags low_delay \
    -rw_timeout 5000000 \
    -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 2 \
    -f mjpeg -use_wallclock_as_timestamps 1 -framerate "$FPS" -i "$XIAO_URL" \
    -an -vf "fps=${FPS},format=yuv420p" \
    "${ENC[@]}" \
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
  if [[ "$MODE" == "stream" ]]; then run_stream_fg; else run_capture_fg; fi
}

if [[ "$MODE" == "capture" ]]; then
  curl -fsS --max-time 3 -o /dev/null "$CAPTURE_URL" \
    && echo "capture OK" >&2 \
    || echo "WARN: capture not reachable" >&2
fi

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  exit $?
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') restart in ${RETRY_S}s…" >&2
  sleep "$RETRY_S"
done
