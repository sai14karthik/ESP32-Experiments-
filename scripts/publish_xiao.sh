#!/usr/bin/env bash
# Bridge XIAO CameraWebServerWiFi MJPEG into MediaMTX path cam_xiao.
#
# Prerequisites:
#   - XIAO on same Wi-Fi, sketch serving MJPEG (ports 80 page / 81 stream)
#   - Terminal A:  ./scripts/mediamtx_run.sh
#   - Terminal B:  ./scripts/publish_xiao.sh [http://XIAO_IP:81/stream]
#
# ESP MJPEG often stalls after ~1 min. This script:
#   1) kills ffmpeg if no encoded progress for STALL_S seconds
#   2) restarts a fresh publish (new HTTP connection to the ESP)
#
# One-shot:  PUBLISH_ONCE=1 ./scripts/publish_xiao.sh …
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:8554/cam_xiao}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
FPS="${XIAO_FPS:-10}"
BITRATE="${XIAO_BITRATE:-400k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-12}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi

if [[ -z "$XIAO_URL" ]]; then
  cat >&2 <<'EOF'
Usage:
  ./scripts/publish_xiao.sh http://<xiao-ip>:81/stream

Or:
  XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/publish_xiao.sh
EOF
  exit 2
fi

echo "Bridging XIAO MJPEG → MediaMTX (stall-watchdog ${STALL_S}s + auto-restart)" >&2
echo "  source: $XIAO_URL" >&2
echo "  dest:   $MTX_URL" >&2
echo "  fps=$FPS bitrate=$BITRATE" >&2
echo "Ctrl+C to stop. Do NOT also open ESP /stream in a browser." >&2

run_once() {
  local progress fpid last last_change now out
  progress="$(mktemp -t xiao_pub_XXXXXX)"
  # shellcheck disable=SC2064
  trap 'rm -f "$progress"; [[ -n "${fpid:-}" ]] && kill "$fpid" 2>/dev/null || true' RETURN

  # Fresh TCP each run. No ffmpeg HTTP reconnect (that half-dead hang is why
  # refresh stops working — MediaMTX still sees a "publisher" with no frames).
  ffmpeg -hide_banner -loglevel error \
    -nostats \
    -progress "$progress" \
    -xerror \
    -fflags +nobuffer+genpts+discardcorrupt \
    -flags low_delay \
    -rw_timeout 8000000 \
    -f mjpeg \
    -use_wallclock_as_timestamps 1 \
    -r "$FPS" \
    -i "$XIAO_URL" \
    -an \
    -c:v libx264 \
    -preset veryfast \
    -tune zerolatency \
    -pix_fmt yuv420p \
    -b:v "$BITRATE" \
    -maxrate "$BITRATE" \
    -bufsize "$BITRATE" \
    -g $((FPS * 2)) \
    -bf 0 \
    -f rtsp \
    -rtsp_transport tcp \
    "$MTX_URL" &
  fpid=$!

  last=""
  last_change=$SECONDS
  while kill -0 "$fpid" 2>/dev/null; do
    if [[ -f "$progress" ]]; then
      out="$(grep -E '^out_time_ms=' "$progress" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
      if [[ -n "$out" && "$out" != "$last" ]]; then
        last="$out"
        last_change=$SECONDS
      fi
    fi
    now=$SECONDS
    if (( now - last_change >= STALL_S )); then
      echo "$(date '+%H:%M:%S') stall: no ffmpeg progress for ${STALL_S}s — killing (ESP MJPEG hung)" >&2
      kill "$fpid" 2>/dev/null || true
      sleep 0.5
      kill -9 "$fpid" 2>/dev/null || true
      wait "$fpid" 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
  wait "$fpid" 2>/dev/null || true
  return $?
}

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  exit $?
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') bridge restart in ${RETRY_S}s…" >&2
  sleep "$RETRY_S"
done
