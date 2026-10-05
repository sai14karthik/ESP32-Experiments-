#!/usr/bin/env bash
set -euo pipefail

# Apple BLE scan serial log. Quit with Ctrl+]
# Usage: ./scripts/monitor_apple_ble.sh
#        ./scripts/monitor_apple_ble.sh /dev/cu.usbserial-10
# Optional: export IDF_ACTIVATE=/path/to/activate_idf_v6.0.2.sh

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/firmware/apple_ble_scan"

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
  echo "Install ESP-IDF 6.0.x (Espressif IDE / eim), then either:"
  echo "  export IDF_ACTIVATE=\"\$HOME/.espressif/tools/activate_idf_v6.0.2.sh\""
  echo "or put export.sh on PATH via: . \$HOME/esp/esp-idf/export.sh"
  exit 1
fi

if ! PORT="$(pick_usb_serial "${1:-}")"; then
  echo "No USB serial port found."
  echo "  macOS:  ls /dev/cu.usb*"
  echo "  Linux:  ls /dev/ttyUSB* /dev/ttyACM*"
  exit 1
fi

if [[ ! -d "$PROJECT" ]]; then
  echo "Missing Apple BLE project: $PROJECT"
  exit 1
fi

export IDF_ACTIVATE PROJECT PORT
exec bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  cd "$PROJECT"
  echo "Monitor $PORT — quit with Ctrl+]"
  idf.py -p "$PORT" monitor
'
