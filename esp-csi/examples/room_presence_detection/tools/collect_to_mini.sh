#!/usr/bin/env bash
# On the Mac Mini (Ethernet). The master pushes to port 9056.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
PY="$HERE/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$HERE/.venv"
    uv pip install --python "$PY" "psycopg[binary]"
  else
    python3 -m venv "$HERE/.venv"
    "$PY" -m pip install "psycopg[binary]"
  fi
fi
exec "$PY" "$HERE/collect_to_mini.py" --listen 9056 "$@"
