#!/usr/bin/env bash
# List USB serial devices and show configured TX/RX roles.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "=== USB serial (cu.*) ==="
shopt -s nullglob
ports=(/dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.SLAB* /dev/cu.wchusbserial*)
if ((${#ports[@]})); then
  printf '%s\n' "${ports[@]}"
else
  echo "(none found)"
fi
echo
echo "=== configs/ports.toml ==="
cat "$ROOT/configs/ports.toml"
if [[ -f "$ROOT/configs/ports.local.toml" ]]; then
  echo
  echo "=== configs/ports.local.toml (override) ==="
  cat "$ROOT/configs/ports.local.toml"
fi
echo
echo "Tip: open serial and look for CSI_DATA lines — that board is RX."
echo "  screen /dev/cu.usbmodem1101 115200"
