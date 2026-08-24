#!/usr/bin/env bash
set -euo pipefail

# Official IDF CSI serial monitor. Quit with Ctrl+]
# Usage: ./monitor_csi.sh
#        ./monitor_csi.sh /dev/cu.usbmodem2101

IDF_ACTIVATE="/Users/saikarthik/.espressif/tools/activate_idf_v6.0.2.sh"
PROJECT="/Users/saikarthik/Desktop/camera_module/esp-csi/examples/get-started/csi_recv_router"

if [[ -n "${1:-}" ]]; then
  PORT="$1"
elif [[ -e /dev/cu.usbmodem2101 ]]; then
  PORT=/dev/cu.usbmodem2101
else
  PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${PORT:-}" || ! -e "$PORT" ]]; then
  echo "No USB serial port found. Plug in the C5, then run: ls /dev/cu.usbmodem*"
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
