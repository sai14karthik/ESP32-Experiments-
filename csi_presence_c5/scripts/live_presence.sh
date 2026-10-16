#!/usr/bin/env bash
# Live EMPTY/OCCUPIED from CSI TCP :9055 (no YOLO).
# Stop collect_session first — port exclusive.
#   ./scripts/live_presence.sh
#   ./scripts/live_presence.sh --seconds 300
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
exec uv run csi-live-presence "$@"
