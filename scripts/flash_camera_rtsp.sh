#!/usr/bin/env bash
# Flash CameraRTSPWiFi (XIAO Sense) — uses Micro-RTSP + pins matching esp32cam-rtsp XIAO board.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQBN="${FQBN:-esp32:esp32:XIAO_ESP32S3:PSRAM=opi,PartitionScheme=default_8MB,CDCOnBoot=default,UploadSpeed=921600}"
SKETCH="$ROOT/firmware/CameraRTSPWiFi"
PORT="${1:-${XIAO_PORT:-/dev/cu.usbmodem101}}"

if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "arduino-cli not found" >&2
  exit 1
fi
if [[ ! -d "$SKETCH/Micro-RTSP" ]]; then
  echo "missing Micro-RTSP submodule under $SKETCH" >&2
  exit 1
fi

echo "compile $SKETCH → $PORT" >&2
arduino-cli compile --fqbn "$FQBN" --library "$SKETCH/Micro-RTSP" "$SKETCH"
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
echo "FLASH_OK — wait for serial: RTSP: rtsp://…:554/mjpeg/1" >&2
