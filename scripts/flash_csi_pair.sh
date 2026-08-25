#!/usr/bin/env bash
set -euo pipefail

# Method 2: flash ESP-NOW CSI pair (csi_send + csi_recv) to two ESP32-C5 boards.
# No Wi-Fi router needed for the CSI link.
#
# Usage (from repo root):
#   ./scripts/flash_csi_pair.sh
#   ./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101
#
# First port = sender (csi_send), second = receiver (csi_recv / plotter).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

SEND_DIR="$ROOT/esp-csi/examples/get-started/csi_send"
RECV_DIR="$ROOT/esp-csi/examples/get-started/csi_recv"

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found."
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
    echo "  Plug both C5 boards in (use the USB/JTAG port if dual-USB)."
    echo "  macOS: ls /dev/cu.usb*"
    return 1
  fi
  printf '%s\n%s\n' "${ports[0]}" "${ports[1]}"
}

# Bash 3.2 (macOS /bin/bash) has no mapfile — use a temp read.
SEND_PORT=""
RECV_PORT=""
while IFS= read -r line; do
  if [[ -z "$SEND_PORT" ]]; then
    SEND_PORT="$line"
  elif [[ -z "$RECV_PORT" ]]; then
    RECV_PORT="$line"
  fi
done < <(pick_two_ports "${1:-}" "${2:-}")
if [[ -z "$SEND_PORT" || -z "$RECV_PORT" ]]; then
  exit 1
fi

echo "Sender   (csi_send): $SEND_PORT"
echo "Receiver (csi_recv): $RECV_PORT"
echo

# Free ports if monitors are holding them
for p in "$SEND_PORT" "$RECV_PORT"; do
  pids="$(lsof -t "$p" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
  fi
done
sleep 0.4

export IDF_ACTIVATE SEND_DIR RECV_DIR SEND_PORT RECV_PORT
bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  set -euo pipefail
  command -v riscv32-esp-elf-gcc >/dev/null

  echo "=== Build + flash csi_send → $SEND_PORT ==="
  cd "$SEND_DIR"
  if [[ ! -f sdkconfig ]] || ! grep -q "CONFIG_IDF_TARGET_ESP32C5=y" sdkconfig 2>/dev/null; then
    idf.py set-target esp32c5
  fi
  idf.py build flash -p "$SEND_PORT" -b 460800

  echo
  echo "=== Build + flash csi_recv → $RECV_PORT ==="
  cd "$RECV_DIR"
  if [[ ! -f sdkconfig ]] || ! grep -q "CONFIG_IDF_TARGET_ESP32C5=y" sdkconfig 2>/dev/null; then
    idf.py set-target esp32c5
  fi
  idf.py build flash -p "$RECV_PORT" -b 460800
'

echo
echo "Done. Place boards >1 m apart, then:"
echo "  ./plot_csi.sh $RECV_PORT"
echo "  # or: ./monitor_csi.sh is for router mode — for this pair use:"
echo "  # idf.py -p $RECV_PORT monitor   (from csi_recv dir, after IDF activate)"
echo
echo "Sender MAC expected by recv filter: 1a:00:00:00:00:00"
echo "Channel: 11, HT40, ~100 Hz ESP-NOW"
