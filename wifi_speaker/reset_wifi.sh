#!/bin/sh
# Wipe saved Wi-Fi on the board so ESP32-AirPlay-Setup comes back.
# Does not reflash firmware.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PORT=${1:-/dev/cu.usbmodem101}
ESPTOOL=${ESPTOOL:-"$HOME/.espressif/tools/python/v6.0.2/venv/bin/esptool"}

if [ ! -x "$ESPTOOL" ]; then
  echo "esptool not found: $ESPTOOL" >&2
  exit 1
fi

IDENT=$("$ESPTOOL" --port "$PORT" chip-id 2>&1 || true)
printf '%s\n' "$IDENT"
if ! printf '%s\n' "$IDENT" | grep -q "ESP32-S3"; then
  echo "Refusing: $PORT is not an ESP32-S3." >&2
  exit 1
fi

"$ESPTOOL" --port "$PORT" erase-region 0x9000 0x10000
"$ESPTOOL" --port "$PORT" --after hard-reset chip-id >/dev/null 2>&1 || true

echo "Wi-Fi cleared on $PORT"
echo "Join hotspot ESP32-AirPlay-Setup, then open http://192.168.4.1"
