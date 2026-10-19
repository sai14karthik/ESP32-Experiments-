#!/bin/sh
# Flash a speaker image that is already in speakers/firmware/<name>/.
# Does not erase saved Wi-Fi.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
NAME=${1:-katie}
PORT_ARG=${2:-}

if [ -f "$ROOT/$NAME.env" ]; then
  # shellcheck disable=SC1090
  . "$ROOT/$NAME.env"
fi
PORT=${PORT_ARG:-${PORT:-/dev/cu.usbmodem101}}
FW="$ROOT/firmware/$NAME"

for f in bootloader.bin partition-table.bin ota_data_initial.bin xiaozhi.bin assets.bin; do
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
  echo "Refusing to flash: $PORT is not an ESP32-S3 audio board." >&2
  exit 1
fi
FLASH_ID=$("$ESPTOOL" --port "$PORT" flash-id 2>&1 || true)
printf '%s\n' "$FLASH_ID"
if ! printf '%s\n' "$FLASH_ID" | grep -q "Detected flash size: 16MB"; then
  echo "Refusing to flash: this image needs 16MB flash." >&2
  exit 1
fi

"$ESPTOOL" --port "$PORT" --baud 460800 --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-freq 80m --flash-size 16MB \
  0x0 "$FW/bootloader.bin" \
  0x8000 "$FW/partition-table.bin" \
  0xd000 "$FW/ota_data_initial.bin" \
  0x20000 "$FW/xiaozhi.bin" \
  0x800000 "$FW/assets.bin"

echo "Flashed $NAME from $FW on $PORT"
