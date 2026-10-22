#!/usr/bin/env bash
# On the Mac Mini. Master pushes status to :9056. YOLO on the ESP camera
# writes empty/occupied ground truth onto each row.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
export PYTHONPATH="$REPO/csi_presence_c5/src${PYTHONPATH:+:$PYTHONPATH}"
export POSE_CONF="${POSE_CONF:-0.55}"
PY="$REPO/csi_presence_c5/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing $PY" >&2
  echo "On the Mini: cd $REPO/csi_presence_c5 && uv sync" >&2
  exit 1
fi
exec "$PY" "$HERE/collect_to_mini.py" --listen 9056 "$@"
