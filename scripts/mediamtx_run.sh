#!/usr/bin/env bash
# Best one-command RTSP pipeline (Mini):
#   XIAO HTTP MJPEG → ffmpeg H.264 → MediaMTX → VLC
#
#   ./scripts/mediamtx_run.sh
# Optional:
#   XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
XIAO_URL="${XIAO_MJPEG_URL:-http://10.128.93.25:81/stream}"

if [[ "$XIAO_URL" != http://* ]]; then
  echo "Need http:// ESP MJPEG (CameraWebServerWiFi :81/stream). Got: $XIAO_URL" >&2
  exit 2
fi
if ! command -v mediamtx >/dev/null 2>&1; then
  echo "Install: brew install mediamtx" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Install: brew install ffmpeg" >&2
  exit 1
fi
if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run: pkill -f mediamtx" >&2
  exit 1
fi

SRC_ESC="$(printf '%s' "$XIAO_URL" | sed 's/[&/\]/\\&/g')"
sed -e "s|__XIAO_MJPEG_URL__|${SRC_ESC}|" "$CONF_SRC" >"$CONF_RT"

echo "Best RTSP pipeline (HTTP MJPEG → ffmpeg H.264 → MediaMTX)" >&2
echo "  ESP   → $XIAO_URL" >&2
echo "  watch → rtsp://127.0.0.1:8554/cam_xiao   (VLC → TCP, cache ~50–100 ms)" >&2
echo "  LAN   → rtsp://10.128.93.23:8554/cam_xiao" >&2
echo "  HLS   → http://10.128.93.23:8888/cam_xiao/" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
