#!/usr/bin/env bash
# Flash CameraWebServerWiFi (XIAO Sense) — HTTP MJPEG for MediaMTX ffmpeg path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQBN="${FQBN:-esp32:esp32:XIAO_ESP32S3:PSRAM=opi,PartitionScheme=default_8MB,CDCOnBoot=default,UploadSpeed=921600}"
SKETCH="$ROOT/firmware/CameraWebServerWiFi"
PORT="${1:-${XIAO_PORT:-/dev/cu.usbmodem101}}"

arduino-cli compile --fqbn "$FQBN" "$SKETCH"
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
echo "FLASH_OK — serial should show: Camera Ready! Use 'http://10.128.93.25'" >&2
