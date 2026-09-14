#!/usr/bin/env bash
# Multi-C5 wireless ingest (method 4.1) — preferred room capture command.
#
# Every powered ESP32-C5 flashed with CSI_TCP_HOST=<this Mac Mini IP> will
# connect to :9055 by itself. This script only starts (or restarts) the fan-in
# listener; it ingests from *all* boards that are up. One board down does not
# stop the others.
#
# On the Mac Mini:
#   ./run_multi_ingest.sh
#   ./run_multi_ingest.sh --label three_rx_smoke
#   ./run_multi_ingest.sh --label occupied_01 --method 4.1
#
# Env (optional):
#   CSI_TCP_PORT=9055     listen port (default 9055)
#   REPLACE_LISTENER=1    kill any existing ingest on that port (default 1)
#
# Extra args are passed through to run_ingest.sh / ingest_serial.py.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CSI_TCP_PORT:-9055}"
REPLACE="${REPLACE_LISTENER:-1}"

LABEL=""
METHOD="4.1"
PASSTHRU=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="${2:-}"; shift 2 ;;
    --label=*)
      LABEL="${1#*=}"; shift ;;
    --method)
      METHOD="${2:-}"; shift 2 ;;
    --method=*)
      METHOD="${1#*=}"; shift ;;
    --listen-tcp|--listen-tcp=*)
      echo "run_multi_ingest.sh already listens on TCP; omit --listen-tcp" >&2
      exit 1 ;;
    *)
      PASSTHRU+=("$1"); shift ;;
  esac
done

if [[ -z "$LABEL" ]]; then
  LABEL="multi_rx_$(date +%Y%m%d_%H%M%S)"
fi

if [[ "$REPLACE" == "1" ]]; then
  if command -v lsof >/dev/null 2>&1; then
    old="$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$old" ]]; then
      echo "replacing listener on :$PORT (pids: $old)"
      # shellcheck disable=SC2086
      kill $old 2>/dev/null || true
      sleep 0.5
    fi
  fi
  pkill -f "ingest_serial.py --listen-tcp" 2>/dev/null || true
  sleep 0.3
fi

echo "multi-C5 ingest → 0.0.0.0:$PORT  method=$METHOD  label=$LABEL"
echo "any powered C5 with CSI_TCP_HOST=<this host> will fan in; Ctrl+C stops"
echo

exec "$ROOT/run_ingest.sh" \
  --listen-tcp "$PORT" \
  --method "$METHOD" \
  --label "$LABEL" \
  "${PASSTHRU[@]}"
