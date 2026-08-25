#!/usr/bin/env bash
set -euo pipefail

# Espressif 4.2 — flash CSI-between-devices on two ESP32-C5 boards.
# Both join the same AP and ping the gateway. Sense board measures CSI from peer MAC.
#
# Usage (from repo root):
#   ./scripts/flash_csi_between.sh 'SaiPhone' '123456789'
#   ./scripts/flash_csi_between.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101 /dev/cu.usbmodem2101
#
# If SSID/PASS omitted, reuses csi_recv_router/sdkconfig.defaults.local when present.
# First port = PEER (traffic, MAC 1a:00:00:00:00:0a)
# Second port = SENSE (plot this one)

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/esp-csi/examples/get-started/csi_between_devices"
ROUTER_LOCAL="$ROOT/esp-csi/examples/get-started/csi_recv_router/sdkconfig.defaults.local"
LOCAL="$PROJECT/sdkconfig.defaults.local"

SSID="${1:-}"
PASS="${2:-}"

if [[ -z "$SSID" || -z "$PASS" ]]; then
  if [[ -f "$ROUTER_LOCAL" ]]; then
    SSID="$(sed -n 's/^CONFIG_EXAMPLE_WIFI_SSID="\(.*\)"/\1/p' "$ROUTER_LOCAL" | head -1)"
    PASS="$(sed -n 's/^CONFIG_EXAMPLE_WIFI_PASSWORD="\(.*\)"/\1/p' "$ROUTER_LOCAL" | head -1)"
  fi
fi

if [[ -z "$SSID" || -z "$PASS" ]]; then
  echo "Usage: $0 \"SSID\" \"PASSWORD\" [peer-port] [sense-port]"
  echo "Or create Wi-Fi local config first via ./scripts/set_csi_wifi.sh"
  exit 1
fi

pick_two_ports() {
  if [[ -n "${1:-}" && -n "${2:-}" ]]; then
    printf '%s\n%s\n' "$1" "$2"
    return 0
  fi
  local p ports=()
  shopt -s nullglob
  for p in \
      /dev/cu.usbmodem* \
      /dev/cu.usbserial* \
      /dev/cu.wchusbserial* \
      /dev/ttyUSB* \
      /dev/ttyACM*; do
    [[ -e "$p" ]] || continue
    ports+=("$p")
  done
  if ((${#ports[@]} < 2)); then
    echo "Need 2 serial ports; found ${#ports[@]}."
    return 1
  fi
  printf '%s\n%s\n' "${ports[0]}" "${ports[1]}"
}

PEER_PORT=""
SENSE_PORT=""
while IFS= read -r line; do
  if [[ -z "$PEER_PORT" ]]; then
    PEER_PORT="$line"
  elif [[ -z "$SENSE_PORT" ]]; then
    SENSE_PORT="$line"
  fi
done < <(pick_two_ports "${3:-}" "${4:-}")
if [[ -z "$PEER_PORT" || -z "$SENSE_PORT" ]]; then
  exit 1
fi

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
  exit 1
fi

cat > "$LOCAL" <<EOF
# Local only — do not commit.
CONFIG_EXAMPLE_WIFI_SSID="$SSID"
CONFIG_EXAMPLE_WIFI_PASSWORD="$PASS"
CONFIG_EXAMPLE_WIFI_AUTH_WPA2_PSK=y
EOF
echo "Wrote $LOCAL (SSID=$SSID)"
echo "Peer  (traffic): $PEER_PORT"
echo "Sense (CSI/plot): $SENSE_PORT"

for p in "$PEER_PORT" "$SENSE_PORT"; do
  pids="$(lsof -t "$p" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
  fi
done
sleep 0.4

export IDF_ACTIVATE PROJECT PEER_PORT SENSE_PORT
bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  set -euo pipefail
  command -v riscv32-esp-elf-gcc >/dev/null
  cd "$PROJECT"

  flash_one() {
    local role="$1" port="$2" bdir="$3" defs="$4" sdk="$5"
    echo
    echo "=== role=$role → $port ==="
    rm -rf "$bdir"
    # Isolated sdkconfig per role (do not share ./sdkconfig across PEER/SENSE)
    rm -f "$sdk"
    export SDKCONFIG="$PROJECT/$sdk"
    idf.py -B "$bdir" \
      -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;$defs" \
      set-target esp32c5
    # Sanity: role bit must match
    if [[ "$role" == "PEER" ]]; then
      grep -q 'CONFIG_CSI_BETWEEN_ROLE_PEER=y' "$SDKCONFIG"
    else
      grep -q 'CONFIG_CSI_BETWEEN_ROLE_SENSE=y' "$SDKCONFIG"
    fi
    idf.py -B "$bdir" \
      -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;$defs" \
      build flash -p "$port" -b 460800
  }

  flash_one PEER  "$PEER_PORT"  build-peer  sdkconfig.defaults.peer  sdkconfig.peer
  flash_one SENSE "$SENSE_PORT" build-sense sdkconfig.defaults.sense sdkconfig.sense
'

echo
echo "Done (Espressif 4.2)."
echo "  Peer  MAC 1a:00:00:00:00:0a : $PEER_PORT"
echo "  Sense (plot this)           : $SENSE_PORT"
echo "Enable Wi-Fi \"$SSID\", then:"
echo "  ./plot_csi.sh $SENSE_PORT"
