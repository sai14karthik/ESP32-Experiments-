#!/usr/bin/env bash
# Wipe Postgres CSI/presence tables + local session files (fresh start).
#   ./scripts/reset_collect_data.sh
#   ./scripts/reset_collect_data.sh --yes   # skip confirm
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"

YES=0
for a in "$@"; do
  [[ "$a" == "--yes" || "$a" == "-y" ]] && YES=1
done

echo "Will DELETE:"
echo "  Postgres $DATABASE_URL  →  csi_samples, presence_windows, csi_sessions"
echo "  Local     $ROOT/data/sessions/*"
if [[ "$YES" -ne 1 ]]; then
  read -r -p "Type YES to confirm: " ans
  [[ "$ans" == "YES" ]] || { echo "aborted"; exit 1; }
fi

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 <<'SQL'
TRUNCATE TABLE presence_windows, csi_samples, csi_sessions RESTART IDENTITY CASCADE;
SQL

mkdir -p data/sessions
# remove session folders only (keep data/sessions itself)
find data/sessions -mindepth 1 -maxdepth 1 -exec rm -rf {} +

echo "cleared."
psql "$DATABASE_URL" -c "
SELECT 'csi_sessions' AS t, count(*) FROM csi_sessions
UNION ALL SELECT 'csi_samples', count(*) FROM csi_samples
UNION ALL SELECT 'presence_windows', count(*) FROM presence_windows;
"
echo "local sessions left: $(find data/sessions -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
echo "Next: ./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --session mockhosp_01"
