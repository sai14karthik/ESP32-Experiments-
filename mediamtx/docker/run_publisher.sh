#!/bin/bash
# Camera publisher on the Mini host. Docker's network cannot open the lab
# camera ports; this process can, and it publishes into MediaMTX on :8554.
set -euo pipefail
cd "$(dirname "$0")"
CREDS="${HOME}/Library/Application Support/mediamtx-cameras/credentials.env"
if [[ -f "$CREDS" ]]; then
  set -a
  # This file is created by install_service.sh with owner-only permissions.
  source "$CREDS"
  set +a
fi
export PATH="/opt/homebrew/bin:${PATH}"
export MTX_HOST="${MTX_HOST:-127.0.0.1}"
export RTSP_PORT="${RTSP_PORT:-8554}"
export MEDIAMTX_LAN_IP="${MEDIAMTX_LAN_IP:-10.128.93.13}"
export API_HOST="127.0.0.1"
export API_PORT="${API_PORT:-8090}"
export DATA_PATH="${DATA_PATH:-$PWD/data/cameras.json}"
mkdir -p "$(dirname "$DATA_PATH")"
while true; do
  python3 -u app.py || true
  sleep 2
done
