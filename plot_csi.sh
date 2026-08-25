#!/usr/bin/env bash
set -euo pipefail

# Live CSI amplitude/phase plotter. Stop with Ctrl+C.
# Do not run this at the same time as ./monitor_csi.sh (same USB port).
# Usage: ./plot_csi.sh
#        ./plot_csi.sh /dev/cu.usbserial-10
#        ./plot_csi.sh /dev/ttyUSB0

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

TOOLS="$ROOT/esp-csi/examples/get-started/tools"
VENV_PY="$ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing $VENV_PY"
  echo "From the repo root (after unzip/clone):"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  uv sync"
  exit 1
fi

if ! PORT="$(pick_usb_serial "${1:-}")"; then
  echo "No USB serial port found."
  echo "  macOS:  ls /dev/cu.usb*"
  echo "  Linux:  ls /dev/ttyUSB* /dev/ttyACM*"
  exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof "$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Quit idf.py monitor or ./monitor_csi.sh first (Ctrl+])."
  lsof "$PORT" || true
  exit 1
fi

cd "$TOOLS"
echo "CSI plotter on $PORT — quit with Ctrl+C"
exec "$VENV_PY" csi_data_read_parse.py -p "$PORT"
