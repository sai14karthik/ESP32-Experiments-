#!/usr/bin/env bash
# Simple MediaMTX (no ffmpeg):
#   ./scripts/mediamtx_run.sh
# Optional:
#   XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
XIAO_URL="${XIAO_RTSP_URL:-rtsp://10.128.93.25:554/mjpeg/1}"

if [[ "$XIAO_URL" != rtsp://* ]]; then
  echo "Need an rtsp:// ESP URL (CameraRTSPWiFi). Got: $XIAO_URL" >&2
  exit 2
fi
if ! command -v mediamtx >/dev/null 2>&1; then
  echo "Install: brew install mediamtx" >&2
  exit 1
fi
if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run: pkill -f mediamtx" >&2
  exit 1
fi

SRC_ESC="$(printf '%s' "$XIAO_URL" | sed 's/[&/\]/\\&/g')"
sed -e "s|__XIAO_RTSP_SOURCE__|${SRC_ESC}|" "$CONF_SRC" >"$CONF_RT"

echo "MediaMTX (no ffmpeg)" >&2
echo "  ESP  → $XIAO_URL" >&2
echo "  watch→ rtsp://127.0.0.1:8554/cam_xiao   (VLC, TCP)" >&2
echo "  LAN  → rtsp://10.128.93.23:8554/cam_xiao" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
