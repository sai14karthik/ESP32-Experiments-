#!/usr/bin/env bash
# Check Sense ESP cam (.15) AND CSI C5 boards (USB / TCP :9055).
#   ./scripts/check_esp.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -d .venv ]]; then
  exec uv run python scripts/check_esp.py "$@"
fi
exec python3 scripts/check_esp.py "$@"
