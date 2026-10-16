#!/usr/bin/env bash
# Empty-room calibration → models/site_calibration.joblib
# Leave room EMPTY. Stop collect_session (needs :9055).
#   ./scripts/calibrate_presence.sh
#   ./scripts/calibrate_presence.sh --seconds 120
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
if [[ ! -d .venv ]]; then
  uv sync
fi
exec uv run csi-calibrate-presence "$@"
