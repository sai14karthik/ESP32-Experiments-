#!/usr/bin/env bash
# Quick health check: Sense cam .15 + CSI TCP/USB.
#   ./scripts/check_esp.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -d .venv ]]; then
  exec uv run python scripts/check_esp.py "$@"
fi
exec python3 scripts/check_esp.py "$@"
