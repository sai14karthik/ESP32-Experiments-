#!/usr/bin/env bash
# Pipeline health: Postgres, feature versions, model, calibration, :9055
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
exec uv run csi-doctor "$@"
