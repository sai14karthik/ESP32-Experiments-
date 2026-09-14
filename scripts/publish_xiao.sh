#!/usr/bin/env bash
# Bridge XIAO CameraWebServerWiFi MJPEG into MediaMTX path cam_xiao.
#
# Prerequisites:
#   - XIAO on same Wi-Fi, sketch serving MJPEG (ports 80 page / 81 stream)
#   - Terminal A:  ./scripts/mediamtx_run.sh
#   - Terminal B:  ./scripts/publish_xiao.sh [http://XIAO_IP:81/stream]
#
# Default stream URL matches the ESP CameraWebServer style path.
# Override:  XIAO_MJPEG_URL=http://192.168.x.x:81/stream ./scripts/publish_xiao.sh
#
# Watch:
#   VLC:     rtsp://127.0.0.1:8554/cam_xiao
#   Browser: http://127.0.0.1:8888/cam_xiao/
#
# ESP MJPEG over Wi‑Fi often stalls; this script auto-restarts the bridge.
# One-shot (no restart):  PUBLISH_ONCE=1 ./scripts/publish_xiao.sh …
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:8554/cam_xiao}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
FPS="${XIAO_FPS:-10}"
BITRATE="${XIAO_BITRATE:-400k}"
RETRY_S="${PUBLISH_RETRY_S:-3}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi

if [[ -z "$XIAO_URL" ]]; then
  cat >&2 <<'EOF'
Usage:
  ./scripts/publish_xiao.sh http://<xiao-ip>:81/stream

Or:
  XIAO_MJPEG_URL=http://10.0.0.50:81/stream ./scripts/publish_xiao.sh

Open http://<xiao-ip>/ in a browser first to confirm the camera is up.
EOF
  exit 2
fi

echo "Bridging XIAO MJPEG → MediaMTX (auto-restart on crash)" >&2
echo "  source: $XIAO_URL" >&2
echo "  dest:   $MTX_URL" >&2
echo "  fps=$FPS bitrate=$BITRATE  (PUBLISH_ONCE=1 to disable restart)" >&2
echo "Ctrl+C to stop." >&2

run_once() {
  # HTTP reconnect flags help when LabPSK / ESP briefly drops the MJPEG socket.
  # Bitrate cap keeps RTP under MediaMTX's UDP payload limit (avoids remux churn).
  ffmpeg -hide_banner -loglevel warning \
    -fflags +nobuffer+genpts \
    -flags low_delay \
    -reconnect 1 \
    -reconnect_at_eof 1 \
    -reconnect_streamed 1 \
    -reconnect_delay_max 5 \
    -rw_timeout 15000000 \
    -f mjpeg \
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
    "$MTX_URL"
}

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  exit $?
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') bridge exited — retry in ${RETRY_S}s (ESP stall / MediaMTX timeout)" >&2
  sleep "$RETRY_S"
done
