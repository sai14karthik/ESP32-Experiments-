#!/usr/bin/env bash
# Label distribution from Postgres (empty vs occupied)
#   ./scripts/db_dist.sh
#   ./scripts/db_dist.sh --session mockhosp_01
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
exec uv run csi-db-dist "$@"
