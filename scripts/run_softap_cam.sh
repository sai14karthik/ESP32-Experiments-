#!/usr/bin/env bash
# Smooth SoftAP lab: XIAO SoftAP → ffmpeg H.264 → MediaMTX (RTSP / WebRTC / HLS).
#
# 1) Flash CameraRTSPWiFi SoftAP (done if serial shows XIAO-CAM).
# 2) Mac Wi‑Fi → join "XIAO-CAM" / password 12345678 (Mac IP becomes 192.168.4.2).
# 3) Run this script.
#
# Watch MediaMTX H.264 (not ESP :554 MJPEG directly):
#   rtsp://127.0.0.1:8554/cam_xiao
#   http://127.0.0.1:8889/cam_xiao/
#   http://127.0.0.1:8888/cam_xiao/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ESP_URL="${XIAO_RTSP_URL:-rtsp://192.168.4.1:554/mjpeg/1}"

echo "Checking SoftAP cam at ${ESP_URL}…" >&2
# ESP SoftAP often ignores ICMP — don't require ping.
if ! nc -z -w 3 192.168.4.1 554 2>/dev/null; then
  cat >&2 <<'EOF'
Cannot open TCP 192.168.4.1:554.

- Rejoin Wi‑Fi XIAO-CAM (board may have restarted)
- Confirm: networksetup -getairportnetwork en0
EOF
  exit 1
fi

if ! ffprobe -v error -rtsp_transport tcp -timeout 8000000 -i "$ESP_URL" \
    -show_entries stream=codec_name -of csv=p=0 >/dev/null 2>&1; then
  echo "RTSP probe failed for $ESP_URL — reset cam USB, rejoin XIAO-CAM, retry" >&2
  exit 1
fi
echo "ESP RTSP OK" >&2

pkill -f mediamtx 2>/dev/null || true
pkill -f publish_xiao 2>/dev/null || true
pkill -f 'ffmpeg.*cam_xiao' 2>/dev/null || true
sleep 1

export XIAO_RTSP_URL="$ESP_URL"
export XIAO_FPS="${XIAO_FPS:-10}"
export XIAO_BITRATE="${XIAO_BITRATE:-1200k}"
# Prefer SoftAP client IP for WebRTC ICE when on XIAO-CAM
if [[ -z "${WEBRTC_HOST:-}" ]]; then
  WEBRTC_HOST="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [[ "$WEBRTC_HOST" == 192.168.4.* ]] || WEBRTC_HOST=127.0.0.1
  export WEBRTC_HOST
fi

exec "$ROOT/scripts/mediamtx_run.sh"
