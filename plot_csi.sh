#!/usr/bin/env bash
set -euo pipefail

# Live CSI amplitude/phase plotter. Stop with Ctrl+C.
# Do not run this at the same time as ./monitor_csi.sh (same USB port).
# Usage: ./plot_csi.sh
#        ./plot_csi.sh /dev/cu.usbmodem101

TOOLS="/Users/saikarthik/Desktop/camera_module/esp-csi/examples/get-started/tools"

if [[ -n "${1:-}" ]]; then
  PORT="$1"
elif [[ -e /dev/cu.usbmodem101 ]]; then
  PORT=/dev/cu.usbmodem101
elif [[ -e /dev/cu.usbmodem2101 ]]; then
  PORT=/dev/cu.usbmodem2101
else
  PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${PORT:-}" || ! -e "$PORT" ]]; then
  echo "No USB serial port found. Plug in the C5, then run: ls /dev/cu.usbmodem*"
  exit 1
fi

if lsof "$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Quit idf.py monitor or ./monitor_csi.sh first (Ctrl+])."
  lsof "$PORT" || true
  exit 1
fi

cd "$TOOLS"
echo "CSI plotter on $PORT — quit with Ctrl+C"
exec python3 csi_data_read_parse.py -p "$PORT"
