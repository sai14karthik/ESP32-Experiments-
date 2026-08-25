#!/usr/bin/env bash
set -euo pipefail

# Create/update the repo-local Python venv (.venv) with uv.
# Safe to re-run on any machine after a GitHub ZIP download or clone.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it, then re-run this script:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  # Windows (PowerShell): irm https://astral.sh/uv/install.ps1 | iex"
  exit 1
fi

uv sync
echo
echo "OK — interpreter: $ROOT/.venv/bin/python"
echo "Try:  uv run python -c \"import serial, numpy; print('ok')\""
