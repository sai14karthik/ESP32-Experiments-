#!/usr/bin/env bash
# Live count of ESP32-C5 boards currently connected to Mini CSI ingest.
#
# On the Mac Mini (while ingest is running):
#   ./count_csi_clients.sh           # once
#   ./count_csi_clients.sh --watch   # refresh every 1s — unplug a board → count drops
#
# Only ESTABLISHED TCP peers on :9055 count. Postgres history is optional (--db).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CSI_TCP_PORT:-9055}"
WATCH=0
SHOW_DB=0
INTERVAL="${WATCH_INTERVAL:-1}"

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
    --db) SHOW_DB=1 ;;
    -h|--help)
      echo "Usage: $0 [--watch] [--db]"
      echo "  (default) live TCP clients only"
      echo "  --watch   refresh every ${INTERVAL}s"
      echo "  --db      also show latest session source_id history"
      exit 0
      ;;
  esac
done

live_peers() {
  # Unique remote IPs with ESTABLISHED sockets to :PORT (connected right now)
  lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED 2>/dev/null \
    | awk 'NR>1 {print $NF}' \
    | sed -nE 's/.*->([0-9.]+):[0-9]+.*/\1/p' \
    | sort -u \
    || true
}

show_once() {
  local peers n
  peers="$(live_peers)"
  if [[ -z "${peers}" ]]; then
    n=0
  else
    n="$(printf '%s\n' "$peers" | grep -c . || true)"
  fi

  echo "=== live CSI clients :$PORT  $(date '+%H:%M:%S') ==="

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof missing"
    return 1
  fi

  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ingest listener: UP"
  else
    echo "ingest listener: DOWN  (run ./run_multi_ingest.sh)"
    echo "connected right now: 0"
    return 0
  fi

  echo "connected right now: $n"
  if [[ "$n" -gt 0 ]]; then
    printf '%s\n' "$peers" | while IFS= read -r ip; do
      [[ -n "$ip" ]] || continue
      # how many sockets from this IP (reconnect can briefly show 2)
      socks="$(
        lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED 2>/dev/null \
          | awk 'NR>1 {print $NF}' \
          | sed -nE 's/.*->([0-9.]+):[0-9]+.*/\1/p' \
          | grep -c "^${ip}$" || true
      )"
      echo "  - $ip  (tcp sockets=$socks)"
    done
  else
    echo "  (none — power a C5 or wait for reconnect)"
  fi

  if [[ "$SHOW_DB" -eq 1 ]]; then
    echo
    echo "=== DB history (not live; last multi session) ==="
    if command -v psql >/dev/null 2>&1; then
      psql "$DATABASE_URL" -q -c "
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
" 2>/dev/null || echo "(no session)"
    else
      echo "psql not found"
    fi
  fi
}

if [[ "$WATCH" -eq 1 ]]; then
  echo "watching live connections (Ctrl+C to stop); unplug a board → count should drop"
  echo
  while true; do
    clear 2>/dev/null || printf '\n----------\n'
    show_once || true
    sleep "$INTERVAL"
  done
else
  show_once
fi
