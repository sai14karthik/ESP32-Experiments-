#!/usr/bin/env bash
# MediaMTX + ESP→H.264 bridge (one terminal).
#
# After copying updated scripts to Mini:
#   pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'
#   ./scripts/mediamtx_run.sh
#
# Browsers on LabPSK (try in order):
#   1) HLS    http://10.128.93.23:8888/cam_xiao/     ← most reliable (TCP)
#   2) WebRTC http://10.128.93.23:8889/cam_xiao/     ← needs TCP/UDP 8189
#   3) RTSP   rtsp://10.128.93.23:8554/cam_xiao      ← VLC
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
WRAPPER="$ROOT/mediamtx/run_xiao_publish.sh"
PUBLISH="$ROOT/scripts/publish_xiao.sh"
XIAO_URL="${XIAO_MJPEG_URL:-http://10.128.93.25:81/stream}"
MODE="${PUBLISH_MODE:-capture}"

if ! command -v mediamtx >/dev/null 2>&1; then
  echo "mediamtx not found. brew install mediamtx" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. brew install ffmpeg" >&2
  exit 1
fi
if [[ ! -f "$CONF_SRC" ]]; then
  echo "missing $CONF_SRC" >&2
  exit 1
fi

chmod +x "$PUBLISH" 2>/dev/null || true

if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run:" >&2
  echo "  pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'" >&2
  exit 1
fi

if [[ "${XIAO_EXTERNAL_PUBLISH:-0}" == "1" ]]; then
  INIT_CMD="/usr/bin/true"
  echo "External publish: PUBLISH_MODE=$MODE $PUBLISH $XIAO_URL" >&2
else
  cat >"$WRAPPER" <<EOF
#!/bin/bash
export PUBLISH_ONCE=1
export PUBLISH_MODE="${MODE}"
exec "$PUBLISH" "$XIAO_URL"
EOF
  chmod +x "$WRAPPER"
  INIT_CMD="$WRAPPER"
fi

INIT_ESC="$(printf '%s' "$INIT_CMD" | sed 's/[&/\]/\\&/g')"
sed "s|__CAM_XIAO_RUN_ON_INIT__|${INIT_ESC}|" "$CONF_SRC" >"$CONF_RT"

echo "=== ESP → MediaMTX ===" >&2
echo "ESP: $XIAO_URL ($MODE)" >&2
echo "Watch (LabPSK — try HLS first):" >&2
echo "  HLS    http://10.128.93.23:8888/cam_xiao/" >&2
echo "  WebRTC http://10.128.93.23:8889/cam_xiao/  (needs :8189 TCP/UDP)" >&2
echo "  RTSP   rtsp://10.128.93.23:8554/cam_xiao" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF_RT"
