#!/usr/bin/env bash
# Flash both C5 boards: first port = TX (csi_send), second = RX (csi_recv).
# Usage:
#   ./flash_pair.sh
#   ./flash_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TX_PORT="${1:-/dev/cu.usbmodem101}"
RX_PORT="${2:-/dev/cu.usbmodem1101}"

echo "=== Pair flash TX=$TX_PORT  RX=$RX_PORT ==="
"$HERE/flash_tx.sh" "$TX_PORT"
echo
"$HERE/flash_rx.sh" "$RX_PORT"
echo
echo "Update roles in csi_presence_c5/configs/ports.toml if needed."
echo "Verify RX: screen $RX_PORT 115200"
