#!/usr/bin/env bash
# Flash CameraWebServerWiFiSense (XIAO Sense) — MJPEG + PCM /audio for MediaMTX A/V.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQBN="${FQBN:-esp32:esp32:XIAO_ESP32S3:PSRAM=opi,PartitionScheme=default_8MB,CDCOnBoot=default,UploadSpeed=921600}"
SKETCH="$ROOT/firmware/CameraWebServerWiFiSense"
PORT="${1:-${XIAO_PORT:-/dev/cu.usbmodem101}}"

arduino-cli compile --fqbn "$FQBN" "$SKETCH"
arduino-cli upload -p "$PORT" --fqbn "$FQBN" "$SKETCH"
echo "FLASH_OK — serial should show Mic ready, /audio, Camera Ready! Use 'http://…'" >&2
echo "Mini: SENSE_AV_URL=http://<ip> ./scripts/mediamtx_run.sh" >&2
echo "VLC:  rtsp://10.128.93.13:8554/cam_sense  (TCP, enable audio)" >&2
