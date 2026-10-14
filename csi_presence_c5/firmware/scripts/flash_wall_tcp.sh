#!/usr/bin/env bash
# Flash C5(s) for Mac Mini wall-power collect:
#   LabPSK STA + CSI + TCP forward → CSI_TCP_HOST:9055
# USB is only needed for this flash step; after that boards run on wall power.
#
# Usage (from csi_presence_c5, boards plugged for flash):
#   cp configs/wifi.env.example configs/wifi.env   # edit SSID/PASS + CSI_TCP_HOST
#   ./firmware/scripts/flash_wall_tcp.sh /dev/cu.usbmodem101
#   ./firmware/scripts/flash_wall_tcp.sh /dev/cu.usbmodem1101
#   ./firmware/scripts/flash_wall_tcp.sh   # flash both detected ports
#
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$PROJ/.." && pwd)"
WIFI_ENV="$PROJ/configs/wifi.env"

if [[ ! -f "$WIFI_ENV" ]]; then
  echo "Missing $WIFI_ENV"
  echo "  cp $PROJ/configs/wifi.env.example $WIFI_ENV"
  echo "  # set WIFI_SSID, WIFI_PASSWORD, CSI_TCP_HOST=<Mac Mini LabPSK IP>"
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$WIFI_ENV"; set +a
: "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}"
CSI_TCP_HOST="${CSI_TCP_HOST:-10.128.93.13}"
CSI_TCP_PORT="${CSI_TCP_PORT:-9055}"

pick_ports() {
  if (($# >= 1)); then
    printf '%s\n' "$@"
    return
  fi
  local p
  shopt -s nullglob
  for p in /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.wchusbserial*; do
    [[ -e "$p" ]] || continue
    printf '%s\n' "$p"
  done
}

PORTS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && PORTS+=("$line")
done < <(pick_ports "$@")
if ((${#PORTS[@]} == 0)); then
  echo "No serial ports. Plug C5(s) for flash (USB), then unplug to wall power after."
  exit 1
fi

echo "Wi-Fi SSID=$WIFI_SSID"
echo "CSI TCP → $CSI_TCP_HOST:$CSI_TCP_PORT"
echo "Ports: ${PORTS[*]}"
echo

export CSI_TCP_HOST CSI_TCP_PORT
for PORT in "${PORTS[@]}"; do
  echo "=== flash wall-TCP RX → $PORT ==="
  CSI_TCP_HOST="$CSI_TCP_HOST" CSI_TCP_PORT="$CSI_TCP_PORT" \
    "$REPO/scripts/set_csi_wifi.sh" "$WIFI_SSID" "$WIFI_PASSWORD" "$PORT"
  echo
done

echo "Done. Unplug USB → wall power. Boards join LabPSK and open TCP to Mini."
echo "On Mac Mini:"
echo "  cd csi_presence_c5 && ./scripts/collect_session.sh --seconds 120"
echo "  # CV GT default: http://10.128.93.15:81/stream"
