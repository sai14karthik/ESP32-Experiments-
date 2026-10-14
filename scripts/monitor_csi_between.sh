#!/usr/bin/env bash
set -euo pipefail

# Espressif §4.2 — IDF monitor on the SENSE board (CSI from peer MAC).
# Quit with Ctrl+]
#
# Usage:
#   ./scripts/monitor_csi_between.sh
#   ./scripts/monitor_csi_between.sh /dev/cu.usbserial-110
#
# Peer board only needs power / Wi‑Fi; do not use this on the peer port.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/esp-csi/examples/get-started/csi_between_devices"
BUILD_DIR="$PROJECT/build-sense"

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
  exit 1
fi

if ! PORT="$(pick_usb_serial "${1:-}")"; then
  echo "No USB serial port found (pass the SENSE port explicitly)."
  echo "  After flash_csi_between.sh: second port = sense"
  exit 1
fi

if [[ ! -d "$PROJECT" ]]; then
  echo "Missing project: $PROJECT"
  exit 1
fi

echo "Espressif §4.2 sense monitor → $PORT"
echo "Expect CSI_DATA lines with MAC 1a:00:00:00:00:0a (peer)."
echo "Both boards must be on the same AP (ssid in sdkconfig.defaults.local)."
echo

export IDF_ACTIVATE PROJECT BUILD_DIR PORT
exec bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  set -euo pipefail
  cd "$PROJECT"
  if [[ -d "$BUILD_DIR" ]]; then
    idf.py -B "$BUILD_DIR" -p "$PORT" monitor
  else
    # Fallback if sense build dir missing
    idf.py -p "$PORT" monitor
  fi
'
