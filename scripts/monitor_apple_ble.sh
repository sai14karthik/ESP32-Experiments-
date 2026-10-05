#!/usr/bin/env bash
set -euo pipefail

# Apple BLE scan serial log.
# Usage: ./scripts/monitor_apple_ble.sh              all Apple types
#        ./scripts/monitor_apple_ble.sh --all        same as above
#        ./scripts/monitor_apple_ble.sh --phone      only the nRF Connect phone
#        ./scripts/monitor_apple_ble.sh --all --phone  full log, including every phone
#        ./scripts/monitor_apple_ble.sh --all /dev/cu.usbserial-10
# Quit --all with Ctrl+]. Quit --phone alone with Ctrl+C.
# Optional: export IDF_ACTIVATE=/path/to/activate_idf_v6.0.2.sh

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/firmware/apple_ble_scan"
WANT_ALL=0
WANT_PHONE=0
PORT_ARG=""

for arg in "$@"; do
  case "$arg" in
    --all) WANT_ALL=1 ;;
    --phone) WANT_PHONE=1 ;;
    -h|--help)
      sed -n '3,11p' "$0"
      exit 0
      ;;
    *)
      if [[ -n "$PORT_ARG" ]]; then
        echo "Unknown argument: $arg"
        exit 1
      fi
      PORT_ARG="$arg"
      ;;
  esac
done

if ! PORT="$(pick_usb_serial "$PORT_ARG")"; then
  echo "No USB serial port found."
  echo "  macOS:  ls /dev/cu.usb*"
  echo "  Linux:  ls /dev/ttyUSB* /dev/ttyACM*"
  exit 1
fi

# --all --phone is the full log. --phone alone is the nRF advertiser only.
if [[ "$WANT_PHONE" -eq 1 && "$WANT_ALL" -eq 0 ]]; then
  echo "nRF phone only on $PORT — SCAN phone=1 is open, phone=0 is locked. Quit with Ctrl+C"
  exec python3 - "$PORT" <<'PY'
import sys
import serial

port = sys.argv[1]
ser = serial.Serial()
ser.port = port
ser.baudrate = 115200
ser.timeout = 0.3
ser.dtr = False
ser.rts = False
ser.open()
try:
    while True:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        if "kind=Phone" in line or "phone=" in line or "listening for all Apple" in line:
            sys.stdout.write(line)
            sys.stdout.flush()
except KeyboardInterrupt:
    pass
finally:
    ser.close()
PY
fi

if [[ ! -d "$PROJECT" ]]; then
  echo "Missing Apple BLE project: $PROJECT"
  exit 1
fi

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
  echo "Install ESP-IDF 6.0.x (Espressif IDE / eim), then either:"
  echo "  export IDF_ACTIVATE=\"\$HOME/.espressif/tools/activate_idf_v6.0.2.sh\""
  echo "or put export.sh on PATH via: . \$HOME/esp/esp-idf/export.sh"
  exit 1
fi

export IDF_ACTIVATE PROJECT PORT
exec bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  cd "$PROJECT"
  echo "Monitor $PORT — all Apple types. Quit with Ctrl+]"
  idf.py -p "$PORT" monitor
'
