#!/usr/bin/env bash
# Start MediaMTX with the lab config in this repo.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$ROOT/mediamtx/mediamtx.yml"

if ! command -v mediamtx >/dev/null 2>&1; then
  echo "mediamtx not found. Install: brew install mediamtx" >&2
  exit 1
fi

if [[ ! -f "$CONF" ]]; then
  echo "missing config: $CONF" >&2
  exit 1
fi

if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 is already in use." >&2
  echo "If Homebrew started MediaMTX:  brew services stop mediamtx" >&2
  exit 1
fi

echo "MediaMTX lab config: $CONF" >&2
echo "  RTSP   rtsp://127.0.0.1:8554/cam1  (webcam)  /cam_xiao (XIAO)" >&2
echo "  HLS    http://127.0.0.1:8888/cam1/" >&2
echo "  WebRTC http://127.0.0.1:8889/cam1/" >&2
echo "Ctrl+C to stop." >&2

exec mediamtx "$CONF"
