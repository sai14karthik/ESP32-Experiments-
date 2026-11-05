#!/bin/bash
# Record a GT session (timestamped joints). macOS needs root for librealsense.
# After one-time ./scripts/install_nopasswd.sh, this needs no password.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing venv. From $ROOT run: uv sync" >&2
  exit 1
fi

if ! sudo -n -l 2>/dev/null | grep -q 'wifi_pose'; then
  echo "Passwordless sudo not set for wifi_pose yet." >&2
  echo "Run once: $ROOT/scripts/install_nopasswd.sh" >&2
  exit 1
fi

exec sudo -n -E "$PY" -m wifi_pose.record "$@"
