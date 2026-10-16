#!/usr/bin/env bash
# Recompute presence_windows.feat from csi_samples after a features.py upgrade.
# Keeps labels. Then retrain.
#   ./scripts/rebuild_features.sh
#   ./scripts/rebuild_features.sh --session-tag mockhosp_01
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
# Apply schema column if needed
psql "$DATABASE_URL" -f schema.sql >/dev/null
exec uv run csi-rebuild-features "$@"
