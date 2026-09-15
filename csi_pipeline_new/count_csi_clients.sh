#!/usr/bin/env bash
# Live count of devices currently feeding Mini CSI TCP ingest.
#
# Counts any client that has an ESTABLISHED TCP session to :9055 and/or has
# inserted samples recently — method 4.1 multi-RX today; later 4.3 multi-RX
# (1+ ESP senders, N ESP receivers) the same way: only boards that *forward*
# CSI_DATA to Mini appear here. ESP senders (power/radio only) do not.
#
# Works on any Wi‑Fi (LabPSK, home, hotel, …) as long as CSI_TCP_HOST points
# at this Mini and clients can reach :9055.
#
# On the Mac Mini (while ingest is running):
#   ./count_csi_clients.sh
#   ./count_csi_clients.sh --watch
#   ./count_csi_clients.sh --verbose
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CSI_TCP_PORT:-9055}"
WATCH=0
VERBOSE=0
INTERVAL="${WATCH_INTERVAL:-1}"
ACTIVE_S="${ACTIVE_WITHIN_S:-10}"

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
    --verbose|-v) VERBOSE=1 ;;
    -h|--help)
      echo "Usage: $0 [--watch] [--verbose]"
      echo "  Shows devices actively ingesting (rows in last ${ACTIVE_S}s) + live TCP."
      exit 0
      ;;
  esac
done

listener_up() {
  if netstat -an -p tcp 2>/dev/null | grep -E "[\.]$PORT .*LISTEN" >/dev/null 2>&1; then
    return 0
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# Remote IPs with ESTABLISHED TCP to local :PORT (macOS netstat format).
tcp_peers() {
  # tcp4  0  0  10.128.93.23.9055  10.128.93.29.61289  ESTABLISHED
  netstat -an -p tcp 2>/dev/null \
    | awk -v p=".$PORT" '
        $1 ~ /^tcp/ && $6 == "ESTABLISHED" {
          local=$4; remote=$5
          if (index(local, p) || index(remote, p)) {
            # peer is the side that is NOT :PORT
            split(local, a, ".")
            split(remote, b, ".")
            # last field is port; IP is fields 1..n-1
            n=split(local, L, ".")
            m=split(remote, R, ".")
            lport=L[n]; rport=R[m]
            lip=L[1]; for(i=2;i<n;i++) lip=lip "." L[i]
            rip=R[1]; for(i=2;i<m;i++) rip=rip "." R[i]
            if (lport == "'"$PORT"'") print rip
            else if (rport == "'"$PORT"'") print lip
          }
        }' \
    | sort -u \
    || true
}

show_once() {
  echo "=== CSI live forwarders  :$PORT  $(date '+%H:%M:%S') ==="

  if listener_up; then
    echo "ingest listener: UP"
  else
    echo "ingest listener: DOWN  (run ./run_multi_ingest.sh)"
  fi

  peers="$(tcp_peers)"
  if [[ -z "${peers}" ]]; then
    tcp_n=0
  else
    tcp_n="$(printf '%s\n' "$peers" | grep -c . || true)"
  fi
  echo "TCP connected right now: $tcp_n"
  if [[ "$tcp_n" -gt 0 ]]; then
    printf '%s\n' "$peers" | while IFS= read -r ip; do
      [[ -n "$ip" ]] && echo "  - $ip"
    done
  fi

  if [[ "$VERBOSE" -eq 1 ]]; then
    echo
    echo "--- netstat :$PORT ---"
    netstat -an -p tcp 2>/dev/null | grep -E "[\.]$PORT" || echo "(none)"
    echo "--- lsof :$PORT ---"
    lsof -nP -iTCP:"$PORT" 2>/dev/null || echo "(none)"
  fi

  echo
  echo "actively ingesting (samples in last ${ACTIVE_S}s):"
  if ! command -v psql >/dev/null 2>&1; then
    echo "  (psql not found)"
    return 0
  fi

  # This matches "ingestion is happening" even if TCP listing fails.
  out="$(
    psql "$DATABASE_URL" -q -t -A -F$'\t' -c "
SELECT source_id,
       count(*)::text,
       to_char(max(host_ts), 'HH24:MI:SS')
FROM csi_samples
WHERE host_ts > now() - interval '${ACTIVE_S} seconds'
  AND source_id IS NOT NULL
GROUP BY source_id
ORDER BY source_id;
" 2>/dev/null || true
  )"

  if [[ -z "${out//[[:space:]]/}" ]]; then
    echo "  0 devices (no rows in last ${ACTIVE_S}s)"
  else
    active_n="$(printf '%s\n' "$out" | grep -c . || true)"
    echo "  $active_n device(s)"
    printf '%s\n' "$out" | while IFS=$'\t' read -r sid cnt ts; do
      [[ -n "${sid:-}" ]] || continue
      echo "  - $sid  (+$cnt rows, last $ts)"
    done
  fi
}

if [[ "$WATCH" -eq 1 ]]; then
  echo "watching (Ctrl+C to stop); unplug a board → active count should drop within ~${ACTIVE_S}s"
  echo
  while true; do
    clear 2>/dev/null || printf '\n----------\n'
    show_once || true
    sleep "$INTERVAL"
  done
else
  show_once
fi
