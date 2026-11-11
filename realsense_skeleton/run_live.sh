#!/bin/bash
# Kinect-style live view: color skeleton beside a 3D stick figure.
# Uses passwordless sudo for this script only (see install_nopasswd.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/bin/python"
SCRIPT="$ROOT/live_joints.py"

if ! sudo -n -l 2>/dev/null | grep -q 'realsense_skeleton/live_joints.py'; then
  echo "Passwordless access is not set up yet (macOS still needs root for the RealSense)." >&2
  echo "Run once (asks for password one time):" >&2
  echo "  $ROOT/install_nopasswd.sh" >&2
  exit 1
fi

exec sudo -n -E "$PY" -u "$SCRIPT" "$@"
