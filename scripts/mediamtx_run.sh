#!/usr/bin/env bash
# MediaMTX + ffmpeg: ESP MJPEG → H.264 → cam_xiao (ffplay/VLC-friendly).
#
#   XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
#
# Watch: ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/cam_xiao
# Board: CameraWebServerWiFi (:81/stream)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
WRAPPER="$ROOT/mediamtx/run_xiao_publish.sh"
PUBLISH="$ROOT/scripts/publish_xiao.sh"
XIAO_URL="${XIAO_MJPEG_URL:-${XIAO_RTSP_URL:-http://10.128.93.25:81/stream}}"

detect_lan_ip() {
  local ip=""
  ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [[ "$ip" == 192.168.4.* ]]; then
    printf '%s' "$ip"
    return 0
  fi
  if [[ -z "$ip" || "$ip" == 192.0.0.* || "$ip" == 169.254.* ]]; then
    ip="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [[ -z "$ip" || "$ip" == 192.0.0.* || "$ip" == 169.254.* ]]; then
    ip="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' | while read -r ifc; do
      ipconfig getifaddr "$ifc" 2>/dev/null && break
    done || true)"
  fi
  if [[ -z "$ip" || "$ip" == 192.0.0.* || "$ip" == 169.254.* ]]; then
    ip="127.0.0.1"
  fi
  printf '%s' "$ip"
}

LAN_IP="${LAN_IP:-$(detect_lan_ip)}"

if ! command -v mediamtx >/dev/null 2>&1; then
  echo "mediamtx not found. Install: brew install mediamtx" >&2
  exit 1
fi
if [[ ! -f "$CONF_SRC" ]]; then
  echo "missing config: $CONF_SRC" >&2
  exit 1
fi

chmod +x "$PUBLISH" 2>/dev/null || true

if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run: pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'" >&2
  exit 1
fi

cat >"$WRAPPER" <<EOF
#!/bin/bash
export PUBLISH_ONCE=1
export XIAO_FPS="\${XIAO_FPS:-15}"
export XIAO_BITRATE="\${XIAO_BITRATE:-2500k}"
exec "$PUBLISH" "$XIAO_URL"
EOF
chmod +x "$WRAPPER"

INIT_ESC="$(printf '%s' "$WRAPPER" | sed 's/[&/\]/\\&/g')"
sed -e "s|__CAM_XIAO_RUN_ON_INIT__|${INIT_ESC}|" "$CONF_SRC" >"$CONF_RT"

echo "MediaMTX (ffmpeg H.264 — works with ffplay/VLC)" >&2
echo "  ESP  : $XIAO_URL" >&2
echo "  serve: rtsp://127.0.0.1:8554/cam_xiao" >&2
if [[ "$LAN_IP" != "127.0.0.1" ]]; then
  echo "  LAN  : rtsp://$LAN_IP:8554/cam_xiao" >&2
fi
echo "  watch: ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/cam_xiao" >&2
echo "Wait for: first frame OK" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
