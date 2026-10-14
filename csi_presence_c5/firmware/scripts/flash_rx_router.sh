#!/usr/bin/env bash
# Optional: LabPSK router-ping CSI (method 4.1 style) via csi_recv_router on C5.
# Requires configs/wifi.env with WIFI_SSID / WIFI_PASSWORD.
# Usage: ./flash_rx_router.sh [/dev/cu.usbmodem1101]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$PROJ/.." && pwd)"
# shellcheck source=/dev/null
source "$REPO/scripts/serial_helpers.sh"

PORT="${1:-/dev/cu.usbmodem1101}"
WIFI_ENV="$PROJ/configs/wifi.env"
RECV_DIR="$REPO/esp-csi/examples/get-started/csi_recv_router"

if [[ ! -f "$WIFI_ENV" ]]; then
  echo "Missing $WIFI_ENV — copy from wifi.env.example and set LabPSK credentials."
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$WIFI_ENV"; set +a
: "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}"

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
  exit 1
fi

pids="$(lsof -t "$PORT" 2>/dev/null || true)"
if [[ -n "$pids" ]]; then
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 0.4
fi

CHIP="$(
  bash --noprofile --norc -c '
    set +u; source "'"$IDF_ACTIVATE"'"; set -euo pipefail
    python -m esptool --port "'"$PORT"'" chip-id 2>&1 | grep -oE "ESP32-C[0-9]+" | head -1
  ' || true
)"
case "${CHIP:-}" in
  ESP32-C5) TARGET=esp32c5 ;;
  ESP32-C6) TARGET=esp32c6 ;;
  *) TARGET=esp32c5 ;;
esac
BUILD_DIR="$RECV_DIR/build_${TARGET}_presence"

echo "RX router (csi_recv_router / $TARGET) SSID=$WIFI_SSID → $PORT"
export IDF_ACTIVATE RECV_DIR BUILD_DIR PORT TARGET WIFI_SSID WIFI_PASSWORD
bash --noprofile --norc -c '
  set +u
  source "$IDF_ACTIVATE"
  set -euo pipefail
  cd "$RECV_DIR"
  if [[ ! -f "$BUILD_DIR/config.env" ]] || ! grep -qi "$TARGET" "$BUILD_DIR/config.env" 2>/dev/null; then
    idf.py -B "$BUILD_DIR" set-target "$TARGET"
  fi
  if [[ -f sdkconfig.defaults ]]; then
    {
      echo ""
      echo "# csi_presence_c5 LabPSK"
      echo "CONFIG_CSI_WIFI_SSID=\"${WIFI_SSID}\""
      echo "CONFIG_CSI_WIFI_PASSWORD=\"${WIFI_PASSWORD}\""
    } > sdkconfig.defaults.presence_c5
  fi
  idf.py -B "$BUILD_DIR" build flash -p "$PORT" -b 460800
'
echo "Done. If SSID was not picked up by Kconfig, use: $REPO/scripts/set_csi_wifi.sh"
