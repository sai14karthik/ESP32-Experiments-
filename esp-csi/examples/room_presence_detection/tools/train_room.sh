#!/usr/bin/env bash
# On the Mac Mini. Reads CSI already stored in Postgres, including an open collect.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
PY="$REPO/csi_presence_c5/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing $PY" >&2
  echo "On the Mini: cd $REPO/csi_presence_c5 && uv sync" >&2
  exit 1
fi
exec "$PY" "$HERE/train_room.py" "$@"
