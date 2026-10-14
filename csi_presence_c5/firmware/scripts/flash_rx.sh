#!/usr/bin/env bash
# Flash ESP-NOW CSI receiver (csi_recv) for ESP32-C5 — prints CSI_DATA on USB.
# Auto-detects C5/C6. Usage: ./flash_rx.sh [/dev/cu.usbmodem1101]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$PROJ/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO/scripts/serial_helpers.sh"

PORT="${1:-/dev/cu.usbmodem1101}"
RECV_DIR="$REPO/esp-csi/examples/get-started/csi_recv"

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found. Install IDF ≥ 5.4 and retry."
  exit 1
fi

pids="$(lsof -t "$PORT" 2>/dev/null || true)"
if [[ -n "$pids" ]]; then
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 0.4
fi

CHIP="$(
  bash --noprofile --norc -c '
    set +u; source "'"$IDF_ACTIVATE"'"; set -euo pipefail
    python -m esptool --port "'"$PORT"'" chip-id 2>&1 | grep -oE "ESP32-C[0-9]+" | head -1
  ' || true
)"
case "${CHIP:-}" in
  ESP32-C5) TARGET=esp32c5 ;;
  ESP32-C6) TARGET=esp32c6 ;;
  *) echo "Could not detect chip on $PORT (got: ${CHIP:-unknown}). Defaulting to esp32c5."; TARGET=esp32c5 ;;
esac
BUILD_DIR="$RECV_DIR/build_${TARGET}_presence"

echo "RX (csi_recv / $TARGET) → $PORT"
export IDF_ACTIVATE RECV_DIR BUILD_DIR PORT TARGET
bash --noprofile --norc -c '
  set +u
  source "$IDF_ACTIVATE"
  set -euo pipefail
  cd "$RECV_DIR"
  if [[ ! -f "$BUILD_DIR/config.env" ]] || ! grep -qi "$TARGET" "$BUILD_DIR/config.env" 2>/dev/null; then
    idf.py -B "$BUILD_DIR" set-target "$TARGET"
  fi
  idf.py -B "$BUILD_DIR" build flash -p "$PORT" -b 460800
'
echo "Done RX. Monitor: screen $PORT 115200   (look for CSI_DATA)"
echo "Then: $PROJ/scripts/collect_session.sh --rx $PORT --baud 115200"
