#!/usr/bin/env bash
# Flash ESP-NOW CSI sender (csi_send) for ESP32-C5 (auto-detects C5/C6).
# Usage: ./flash_tx.sh [/dev/cu.usbmodem101]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$PROJ/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO/scripts/serial_helpers.sh"

PORT="${1:-/dev/cu.usbmodem101}"
SEND_DIR="$REPO/esp-csi/examples/get-started/csi_send"

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

# Detect chip from esptool
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
BUILD_DIR="$SEND_DIR/build_${TARGET}_presence"

echo "TX (csi_send / $TARGET) → $PORT"
export IDF_ACTIVATE SEND_DIR BUILD_DIR PORT TARGET
bash --noprofile --norc -c '
  set +u
  source "$IDF_ACTIVATE"
  set -euo pipefail
  cd "$SEND_DIR"
  if [[ ! -f "$BUILD_DIR/config.env" ]] || ! grep -qi "$TARGET" "$BUILD_DIR/config.env" 2>/dev/null; then
    idf.py -B "$BUILD_DIR" set-target "$TARGET"
  fi
  idf.py -B "$BUILD_DIR" build flash -p "$PORT" -b 460800
'
echo "Done TX. ESP-NOW traffic on channel 11 (~100 Hz)."
