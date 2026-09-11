#!/usr/bin/env bash
# Live CSI visualizer (USB serial → amplitude / RSSI / heatmap).
#
#   ./run_csi_viz.sh
#   ./run_csi_viz.sh --port /dev/cu.usbmodem2101
#   ./run_csi_viz.sh --from-file fixtures/sample_csi_lines.csv
#
# Do not run ingest or detect_gui on the same USB port at the same time.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT/uv_common.sh"

uv_csi "$ROOT/csi_viz_gui.py" "$@"
