#!/bin/sh
# Flash the Wi-Fi AirPlay speaker image (replaces Katie until you flash Katie back).
# Does not keep old Wi-Fi settings: erases NVS so setup AP appears.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PORT=${1:-/dev/cu.usbmodem101}
FW="$ROOT/firmware/wifi_speaker"

for f in bootloader.bin partition-table.bin ota_data_initial.bin firmware.bin spiffs.bin; do
  if [ ! -f "$FW/$f" ]; then
    echo "Missing $FW/$f" >&2
    exit 1
  fi
done

ESPTOOL=${ESPTOOL:-"$HOME/.espressif/tools/python/v6.0.2/venv/bin/esptool"}
if [ ! -x "$ESPTOOL" ]; then
  echo "esptool not found: $ESPTOOL" >&2
  exit 1
fi

IDENT=$("$ESPTOOL" --port "$PORT" chip-id 2>&1 || true)
printf '%s\n' "$IDENT"
if ! printf '%s\n' "$IDENT" | grep -q "ESP32-S3"; then
  echo "Refusing to flash: $PORT is not an ESP32-S3." >&2
  exit 1
fi

# Clear saved Wi-Fi so ESP32-AirPlay-Setup comes up.
"$ESPTOOL" --port "$PORT" erase-region 0x9000 0x10000

"$ESPTOOL" --port "$PORT" --baud 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x0 "$FW/bootloader.bin" \
  0x8000 "$FW/partition-table.bin" \
  0x19000 "$FW/ota_data_initial.bin" \
  0x20000 "$FW/firmware.bin" \
  0x620000 "$FW/spiffs.bin"

echo "Flashed Wi-Fi speaker on $PORT"
echo "Join hotspot ESP32-AirPlay-Setup, open http://192.168.4.1, enter Wi-Fi."
echo "Then AirPlay from iPhone/Mac to ESP32-AirPlay."
echo "Restore Katie later: ./speakers/flash.sh katie"
