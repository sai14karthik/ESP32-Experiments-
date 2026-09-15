#!/usr/bin/env bash
# MediaMTX + ESP bridge — all protocols (RTSP / HLS / WebRTC / RTMP).
#
# Preferred (continuous live — CameraRTSPWiFi / esp32cam-rtsp on the board):
#   XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
#
# Legacy (HTTP /capture polls — choppy HLS "growing clock"):
#   XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
#
# WEBRTC_HOST can be set; otherwise auto-detect this Mac's LAN IP.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
WRAPPER="$ROOT/mediamtx/run_xiao_publish.sh"
PUBLISH="$ROOT/scripts/publish_xiao.sh"
# Prefer on-board RTSP if set; else legacy MJPEG HTTP.
XIAO_URL="${XIAO_RTSP_URL:-${XIAO_MJPEG_URL:-rtsp://10.128.93.25:554/mjpeg/1}}"

detect_lan_ip() {
  local ip=""
  ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
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

WEBRTC_HOST="${WEBRTC_HOST:-$(detect_lan_ip)}"

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
  echo "Port 8554 is already in use." >&2
  echo "  pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'" >&2
  exit 1
fi

if [[ "${XIAO_EXTERNAL_PUBLISH:-0}" == "1" ]]; then
  INIT_CMD="/usr/bin/true"
  echo "XIAO_EXTERNAL_PUBLISH=1 — start publish yourself for: $XIAO_URL" >&2
else
  cat >"$WRAPPER" <<EOF
#!/bin/bash
export PUBLISH_ONCE=1
export XIAO_FPS="\${XIAO_FPS:-8}"
export XIAO_BITRATE="\${XIAO_BITRATE:-800k}"
export PUBLISH_HOLD_LAST="\${PUBLISH_HOLD_LAST:-0}"
# Mode auto-selected from URL scheme inside publish_xiao.sh (rtsp:// → continuous).
exec "$PUBLISH" "$XIAO_URL"
EOF
  chmod +x "$WRAPPER"
  INIT_CMD="$WRAPPER"
fi

INIT_ESC="$(printf '%s' "$INIT_CMD" | sed 's/[&/\]/\\&/g')"
HOST_ESC="$(printf '%s' "$WEBRTC_HOST" | sed 's/[&/\]/\\&/g')"
sed -e "s|__CAM_XIAO_RUN_ON_INIT__|${INIT_ESC}|" \
    -e "s|__WEBRTC_HOST__|${HOST_ESC}|" \
    "$CONF_SRC" >"$CONF_RT"

echo "MediaMTX — all protocols" >&2
echo "  ESP: $XIAO_URL" >&2
echo "  LAN: $WEBRTC_HOST" >&2
echo "  RTSP   rtsp://127.0.0.1:8554/cam_xiao" >&2
echo "  HLS    http://127.0.0.1:8888/cam_xiao/" >&2
echo "  WebRTC http://127.0.0.1:8889/cam_xiao/" >&2
echo "  RTMP   rtmp://127.0.0.1:1935/cam_xiao" >&2
echo "  Same on LAN: replace 127.0.0.1 with $WEBRTC_HOST" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
