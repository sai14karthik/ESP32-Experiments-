#!/usr/bin/env bash
# Train EMPTY/OCCUPIED from Postgres presence_windows → models/presence.joblib
#   ./scripts/train_presence.sh
#   ./scripts/train_presence.sh --session-tag mockhosp_01
#   ./scripts/train_presence.sh --min-per-class 2000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
exec uv run csi-train-presence "$@"
