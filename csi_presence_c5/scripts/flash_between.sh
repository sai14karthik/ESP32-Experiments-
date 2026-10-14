#!/usr/bin/env bash
# Thin wrapper → repo-root Espressif §4.2 flash (peer + sense).
# Usage (from csi_presence_c5):
#   ./scripts/flash_between.sh
#   ./scripts/flash_between.sh /dev/cu.usbmodem101 /dev/cu.usbserial-110
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WIFI_ENV="$(cd "$(dirname "$0")/.." && pwd)/configs/wifi.env"
if [[ -f "$WIFI_ENV" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$WIFI_ENV"; set +a
fi
if [[ $# -ge 2 && -e "${1:-}" && -e "${2:-}" ]]; then
  exec "$ROOT/scripts/flash_csi_between.sh" \
    "${WIFI_SSID:?set WIFI_SSID in configs/wifi.env}" \
    "${WIFI_PASSWORD:?}" \
    "$1" "$2"
fi
exec "$ROOT/scripts/flash_csi_between.sh" "$@"
