#!/bin/sh
# Flash the Wi-Fi AirPlay speaker image onto a Waveshare ESP32-S3-AUDIO-Board.
# Erases saved Wi-Fi so the setup hotspot appears after reboot.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
PORT=${1:-/dev/cu.usbmodem101}
FW="$ROOT/firmware"

for f in bootloader.bin partition-table.bin ota_data_initial.bin firmware.bin spiffs.bin; do
  if [ ! -f "$FW/$f" ]; then
    echo "Missing $FW/$f" >&2
    exit 1
  fi
done

ESPTOOL=${ESPTOOL:-"$HOME/.espressif/tools/python/v6.0.2/venv/bin/esptool"}
if [ ! -x "$ESPTOOL" ]; then
  echo "esptool not found: $ESPTOOL" >&2
  echo "Install ESP-IDF 6.0.2 tools, or set ESPTOOL=/path/to/esptool" >&2
  exit 1
fi

IDENT=$("$ESPTOOL" --port "$PORT" chip-id 2>&1 || true)
printf '%s\n' "$IDENT"
if ! printf '%s\n' "$IDENT" | grep -q "ESP32-S3"; then
  echo "Refusing to flash: $PORT is not an ESP32-S3." >&2
  exit 1
fi
FLASH_ID=$("$ESPTOOL" --port "$PORT" flash-id 2>&1 || true)
printf '%s\n' "$FLASH_ID"
if ! printf '%s\n' "$FLASH_ID" | grep -q "Detected flash size: 16MB"; then
  echo "Refusing to flash: this image needs 16MB flash." >&2
  exit 1
fi

# Clear saved Wi-Fi (AirPlay NVS is 64 KB at 0x9000).
"$ESPTOOL" --port "$PORT" erase-region 0x9000 0x10000

"$ESPTOOL" --port "$PORT" --baud 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x0 "$FW/bootloader.bin" \
  0x8000 "$FW/partition-table.bin" \
  0x19000 "$FW/ota_data_initial.bin" \
  0x20000 "$FW/firmware.bin" \
  0x620000 "$FW/spiffs.bin"

echo
echo "Flashed Wi-Fi speaker on $PORT"
echo "1. Join hotspot: ESP32-AirPlay-Setup  (2.4 GHz, no password)"
echo "2. Open:         http://192.168.4.1"
echo "3. Enter Wi-Fi that your phone also uses"
echo "4. AirPlay to:   ESP32 AirPlay"
echo
echo "Reset Wi-Fi only later:  ./wifi_speaker/reset_wifi.sh"
echo "Restore Katie voice:     ./speakers/flash.sh katie"
