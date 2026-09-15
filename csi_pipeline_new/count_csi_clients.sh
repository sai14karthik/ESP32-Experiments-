#!/usr/bin/env bash
# Show how many ESP32-C5 boards are connected to Mini CSI TCP ingest.
#
# On the Mac Mini:
#   ./count_csi_clients.sh
#   ./count_csi_clients.sh --watch          # refresh every 2s
#   CSI_TCP_PORT=9055 ./count_csi_clients.sh
#
# Live = TCP ESTABLISHED peers on the ingest port (who is connected now).
# DB   = source_id counts for the latest tcp:*:multi session (who has data).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CSI_TCP_PORT:-9055}"
WATCH=0

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$ROOT/.env")
  set +a
fi
DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

for a in "$@"; do
  case "$a" in
    --watch|-w) WATCH=1 ;;
    -h|--help)
      echo "Usage: $0 [--watch]"
      exit 0
      ;;
  esac
done

show_once() {
  echo "=== CSI TCP :$PORT ($(date '+%H:%M:%S')) ==="

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof not found; cannot list live TCP clients"
  else
    if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "listener: UP"
    else
      echo "listener: NOT RUNNING (start ./run_multi_ingest.sh)"
    fi

    # Unique remote IPs with ESTABLISHED sockets to :PORT
    peers="$(
      lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED 2>/dev/null \
        | awk 'NR>1 {print $NF}' \
        | sed -nE 's/.*->([0-9.]+):[0-9]+.*/\1/p' \
        | sort -u
    )" || true
    if [[ -z "${peers:-}" ]]; then
      echo "connected ESP devices (live TCP): 0"
    else
      n="$(printf '%s\n' "$peers" | grep -c . || true)"
      echo "connected ESP devices (live TCP): $n"
      printf '%s\n' "$peers" | while IFS= read -r ip; do
        [[ -n "$ip" ]] && echo "  - $ip"
      done
    fi
  fi

  echo
  echo "=== latest multi session (Postgres) ==="
  if ! command -v psql >/dev/null 2>&1; then
    echo "psql not found; skip DB"
    return 0
  fi

  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "
SELECT
  s.label,
  s.recv_port,
  s.started_at,
  (SELECT count(DISTINCT source_id)
     FROM csi_samples x WHERE x.session_id = s.id AND source_id IS NOT NULL) AS n_devices,
  (SELECT count(*) FROM csi_samples x WHERE x.session_id = s.id) AS n_rows
FROM csi_sessions s
WHERE s.recv_port LIKE 'tcp:%:multi'
ORDER BY s.started_at DESC
LIMIT 1;
" 2>/dev/null || echo "(no multi session yet or DB unreachable)"

  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "
SELECT source_id, count(*) AS rows, max(host_ts) AS last_sample
FROM csi_samples
WHERE session_id = (
  SELECT id FROM csi_sessions
  WHERE recv_port LIKE 'tcp:%:multi'
  ORDER BY started_at DESC LIMIT 1
)
AND source_id IS NOT NULL
GROUP BY source_id
ORDER BY source_id;
" 2>/dev/null || true
}

if [[ "$WATCH" -eq 1 ]]; then
  while true; do
    clear 2>/dev/null || printf '\n'
    show_once
    sleep 2
  done
else
  show_once
fi
