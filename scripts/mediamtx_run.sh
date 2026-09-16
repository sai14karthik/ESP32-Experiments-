#!/usr/bin/env bash
# MediaMTX RTSP-only: pull ESP Micro-RTSP → re-serve on :8554/cam_xiao (no ffmpeg).
#
#   XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
#
# Watch (ffplay):
#   ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://10.128.93.23:8554/cam_xiao
#
# Board must run CameraRTSPWiFi (serial: RTSP: rtsp://…:554/mjpeg/1).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
XIAO_URL="${XIAO_RTSP_URL:-rtsp://10.128.93.25:554/mjpeg/1}"

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

if [[ "$XIAO_URL" != rtsp://* ]]; then
  echo "RTSP-only mode needs XIAO_RTSP_URL=rtsp://… (got: $XIAO_URL)" >&2
  exit 2
fi

if ! command -v mediamtx >/dev/null 2>&1; then
  echo "mediamtx not found. Install: brew install mediamtx" >&2
  exit 1
fi
if [[ ! -f "$CONF_SRC" ]]; then
  echo "missing config: $CONF_SRC" >&2
  exit 1
fi

if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run: pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'" >&2
  exit 1
fi

SRC_ESC="$(printf '%s' "$XIAO_URL" | sed 's/[&/\]/\\&/g')"
sed -e "s|__XIAO_RTSP_SOURCE__|${SRC_ESC}|" "$CONF_SRC" >"$CONF_RT"

echo "MediaMTX (RTSP only — no ffmpeg)" >&2
echo "  pull  : $XIAO_URL" >&2
echo "  serve : rtsp://127.0.0.1:8554/cam_xiao" >&2
if [[ "$LAN_IP" != "127.0.0.1" ]]; then
  echo "  LAN   : rtsp://$LAN_IP:8554/cam_xiao" >&2
fi
echo "  watch : ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay rtsp://${LAN_IP}:8554/cam_xiao" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
