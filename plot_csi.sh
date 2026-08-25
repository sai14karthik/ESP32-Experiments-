#!/usr/bin/env bash
set -euo pipefail

# Live CSI amplitude/phase plotter. Stop with Ctrl+C.
# Do not run this at the same time as ./monitor_csi.sh (same USB port).
# Usage: ./plot_csi.sh
#        ./plot_csi.sh /dev/cu.usbserial-10

ROOT="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$ROOT/esp-csi/examples/get-started/tools"
VENV_PY="$ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing $VENV_PY — from the repo root run: uv sync"
  exit 1
fi

pick_usb_serial() {
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
    return
  fi
  local p
  shopt -s nullglob
  for p in /dev/cu.usbserial* /dev/cu.wchusbserial* /dev/cu.SLAB_USBtoUART /dev/cu.usbmodem*; do
    [[ -e "$p" ]] || continue
    printf '%s\n' "$p"
    return
  done
}

PORT="$(pick_usb_serial "${1:-}")"

if [[ -z "${PORT:-}" || ! -e "$PORT" ]]; then
  echo "No USB serial port found. Plug in the C5, then run: ls /dev/cu.usb*"
  exit 1
fi

if lsof "$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Quit idf.py monitor or ./monitor_csi.sh first (Ctrl+])."
  lsof "$PORT" || true
  exit 1
fi

cd "$TOOLS"
echo "CSI plotter on $PORT — quit with Ctrl+C"
exec "$VENV_PY" csi_data_read_parse.py -p "$PORT"
