#!/usr/bin/env bash
set -euo pipefail

# Official IDF CSI serial monitor. Quit with Ctrl+]
# Usage: ./monitor_csi.sh
#        ./monitor_csi.sh /dev/cu.usbserial-10

IDF_ACTIVATE="/Users/saikarthik/.espressif/tools/activate_idf_v6.0.2.sh"
PROJECT="/Users/saikarthik/Desktop/camera_module/esp-csi/examples/get-started/csi_recv_router"

# C5 boards with a CH340/CP2102 UART show up as cu.usbserial-*, not cu.usbmodem-*.
# Prefer usbserial so a plugged-in XIAO camera (usbmodem) is not picked first.
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

# Espressif's activate script refuses unless \$0 is bash/zsh.
export IDF_ACTIVATE PROJECT PORT
exec bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  cd "$PROJECT"
  echo "Monitor $PORT — quit with Ctrl+]"
  idf.py -p "$PORT" monitor
'
