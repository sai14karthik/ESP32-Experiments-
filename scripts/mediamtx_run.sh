#!/usr/bin/env bash
# Start MediaMTX with ESP → ffmpeg → cam_xiao (official hook pattern).
#
# Canonical (quality / smooth — delay OK):
#   XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
#
# Watch (smooth):   http://<MINI_IP>:8888/cam_xiao/
# Watch (WebRTC):   http://<MINI_IP>:8889/cam_xiao/
# Watch (ffplay):   rtsp://<MINI_IP>:8554/cam_xiao
#
# Refs:
#   https://mediamtx.org/docs/publish/generic-webcams
#   https://mediamtx.org/docs/features/hooks
#   https://mediamtx.org/docs/features/decrease-packet-loss
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
WRAPPER="$ROOT/mediamtx/run_xiao_publish.sh"
PUBLISH="$ROOT/scripts/publish_xiao.sh"
XIAO_URL="${XIAO_RTSP_URL:-${XIAO_MJPEG_URL:-http://10.128.93.25:81/stream}}"

detect_lan_ip() {
  local ip=""
  # Prefer SoftAP client address when Mac is on XIAO-CAM.
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
  echo "Port 8554 in use. Run: pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'" >&2
  exit 1
fi

if [[ "${XIAO_EXTERNAL_PUBLISH:-0}" == "1" ]]; then
  INIT_CMD="/usr/bin/true"
  echo "XIAO_EXTERNAL_PUBLISH=1 — start publish yourself for: $XIAO_URL" >&2
else
  cat >"$WRAPPER" <<EOF
#!/bin/bash
export PUBLISH_ONCE=1
export XIAO_FPS="\${XIAO_FPS:-12}"
export XIAO_BITRATE="\${XIAO_BITRATE:-2500k}"
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

echo "MediaMTX (quality / smooth — a few seconds delay is OK)" >&2
echo "  ESP: $XIAO_URL" >&2
echo "  HLS (smooth browser) → http://127.0.0.1:8888/cam_xiao/" >&2
echo "  WebRTC               → http://127.0.0.1:8889/cam_xiao/" >&2
echo "  RTSP (ffplay)        → rtsp://127.0.0.1:8554/cam_xiao" >&2
if [[ "$WEBRTC_HOST" != "127.0.0.1" ]]; then
  echo "  LAN: replace 127.0.0.1 with $WEBRTC_HOST" >&2
fi
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
