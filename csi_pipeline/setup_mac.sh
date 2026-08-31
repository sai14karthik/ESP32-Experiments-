#!/usr/bin/env bash
# One-shot setup for this Mac or a Mac Mini (Apple Silicon Homebrew).
# Usage: ./setup_mac.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT/uv_common.sh"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install from https://brew.sh then re-run." >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "Installing postgresql@16…"
  brew install postgresql@16
fi

brew services start postgresql@16 >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
  if psql -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='csi'" | grep -q 1; then
  echo "Creating database csi…"
  createdb csi
fi

echo "Applying schema…"
psql -d csi -v ON_ERROR_STOP=1 -f "$ROOT/schema.sql" >/dev/null

echo "Syncing Python deps (uv)…"
ensure_uv

ENV_FILE="$ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  echo "Wrote $ENV_FILE (edit DATABASE_URL if needed)."
fi

echo
echo "Setup OK on $(hostname)."
echo "  DATABASE_URL=postgresql:///csi"
echo "  Python: uv run --group csi (from repo root)"
echo "  Next: ./run_ingest.sh --method 4.3 --channel 11 --label desk"
echo "  Detect: ./run_detect.sh --train --csv \"../sample data /csi_packets.csv\""
