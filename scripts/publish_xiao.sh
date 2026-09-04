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
set -euo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:8554/cam_xiao}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
FPS="${XIAO_FPS:-15}"

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

echo "Bridging XIAO MJPEG → MediaMTX" >&2
echo "  source: $XIAO_URL" >&2
echo "  dest:   $MTX_URL" >&2
echo "Ctrl+C to stop." >&2

# MJPEG over HTTP → H.264 → RTSP publish. Low latency flags for lab use.
exec ffmpeg -hide_banner -loglevel info \
  -fflags nobuffer \
  -flags low_delay \
  -f mjpeg \
  -r "$FPS" \
  -i "$XIAO_URL" \
  -an \
  -c:v libx264 \
  -preset veryfast \
  -tune zerolatency \
  -pix_fmt yuv420p \
  -g $((FPS * 2)) \
  -f rtsp \
  -rtsp_transport tcp \
  "$MTX_URL"
