#!/usr/bin/env bash
# On the Mini. Scores the open collect, or the latest stored session if none is open.
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
exec "$PY" "$HERE/live_room.py" "$@"
