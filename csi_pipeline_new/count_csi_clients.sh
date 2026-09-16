#!/usr/bin/env bash
# Live CSI TCP forwarders on Mini :9055 (method 4.1 multi-RX).
#
# "Live connected" = ESTABLISHED TCP to :9055 (needs *some* listener).
# Postgres "recent samples" is separate — can be 0 while TCP is still up.
#
#   ./count_csi_clients.sh              # once
#   ./count_csi_clients.sh --watch      # refresh
#   ./count_csi_clients.sh --verbose
#   ./count_csi_clients.sh --status-listen   # accept TCP (no Postgres) so
#                                           # boards show as connected without ingest
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${CSI_TCP_PORT:-9055}"
WATCH=0
VERBOSE=0
STATUS_LISTEN=0
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
    --status-listen|--listen-status) STATUS_LISTEN=1 ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--watch] [--verbose] [--status-listen]

  Live connected devices = TCP ESTABLISHED to :$PORT (any listener).
  Recent samples         = Postgres rows in the last ${ACTIVE_S}s (optional).

  --status-listen  Bind :$PORT and accept boards (discard CSI lines, no DB).
                   Use when ingest is not running so TCP connected still works.
                   Do not run alongside ./run_multi_ingest.sh.
EOF
      exit 0
      ;;
  esac
done

listener_up() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  netstat -an -p tcp 2>/dev/null | grep -E "[\.]$PORT .*LISTEN" >/dev/null 2>&1 && return 0
  return 1
}

listener_cmd() {
  # Best-effort process name holding LISTEN on :PORT
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $1; exit}'
}

# Remote IPs with ESTABLISHED TCP involving local :PORT (prefer lsof).
tcp_peers() {
  local out=""
  if command -v lsof >/dev/null 2>&1; then
    # … TCP 10.128.93.23:9055->10.128.93.29:61289 (ESTABLISHED)
    out="$(
      lsof -nP -iTCP:"$PORT" -sTCP:ESTABLISHED 2>/dev/null \
        | awk -v p=":$PORT" '
            NR == 1 { next }
            {
              line = $0
              sub(/^.*TCP /, "", line)
              sub(/ \(.*$/, "", line)
              n = split(line, ends, "->")
              if (n != 2) next
              left = ends[1]; right = ends[2]
              if (index(left, p) > 0) {
                # peer host:port on the right
                if (match(right, /:[0-9]+$/)) print substr(right, 1, RSTART - 1)
              } else if (index(right, p) > 0) {
                if (match(left, /:[0-9]+$/)) print substr(left, 1, RSTART - 1)
              }
            }' \
        | sed '/^$/d' | sort -u
    )"
  fi
  if [[ -z "${out}" ]]; then
    out="$(
      netstat -an -p tcp 2>/dev/null \
        | awk -v port="$PORT" '
            $1 ~ /^tcp/ && $6 == "ESTABLISHED" {
              n=split($4, L, "."); m=split($5, R, ".")
              lport=L[n]; rport=R[m]
              lip=L[1]; for(i=2;i<n;i++) lip=lip "." L[i]
              rip=R[1]; for(i=2;i<m;i++) rip=rip "." R[i]
              if (lport == port) print rip
              else if (rport == port) print lip
            }' \
        | sort -u
    )"
  fi
  printf '%s' "$out"
}

show_once() {
  echo "=== CSI live forwarders  :$PORT  $(date '+%H:%M:%S') ==="

  if listener_up; then
    cmd="$(listener_cmd || true)"
    echo "listener: UP${cmd:+ ($cmd)}"
  else
    echo "listener: DOWN  — boards cannot stay TCP-connected"
    echo "  start ingest:  ./run_multi_ingest.sh"
    echo "  or status only: ./count_csi_clients.sh --status-listen"
  fi

  peers="$(tcp_peers)"
  if [[ -z "${peers}" ]]; then
    tcp_n=0
  else
    tcp_n="$(printf '%s\n' "$peers" | grep -c . || true)"
  fi
  echo
  echo "live connected devices (TCP): $tcp_n"
  if [[ "$tcp_n" -gt 0 ]]; then
    printf '%s\n' "$peers" | while IFS= read -r ip; do
      [[ -n "$ip" ]] && echo "  - $ip"
    done
  fi

  if [[ "$VERBOSE" -eq 1 ]]; then
    echo
    echo "--- lsof :$PORT ---"
    lsof -nP -iTCP:"$PORT" 2>/dev/null || echo "(none)"
  fi

  echo
  echo "recent samples in DB (last ${ACTIVE_S}s) — optional, not required for 'connected':"
  if ! command -v psql >/dev/null 2>&1; then
    echo "  (psql not found)"
    return 0
  fi

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
    echo "  0 (no rows — TCP can still be connected)"
  else
    active_n="$(printf '%s\n' "$out" | grep -c . || true)"
    echo "  $active_n device(s) writing"
    printf '%s\n' "$out" | while IFS=$'\t' read -r sid cnt ts; do
      [[ -n "${sid:-}" ]] || continue
      echo "  - $sid  (+$cnt rows, last $ts)"
    done
  fi
}

run_status_listen() {
  if listener_up; then
    echo "Port $PORT already has a listener ($(listener_cmd || echo unknown))." >&2
    echo "Use ./count_csi_clients.sh --watch with ingest running, or stop ingest first." >&2
    exit 1
  fi
  echo "status listener on :$PORT (accept + discard CSI; no Postgres)" >&2
  echo "In another terminal: ./count_csi_clients.sh --watch" >&2
  echo "Ctrl+C to stop." >&2
  exec python3 - "$PORT" <<'PY'
import socket, sys, threading

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("0.0.0.0", port))
sock.listen(8)
print(f"listening tcp://0.0.0.0:{port} (status-only)", flush=True)

def drain(conn, addr):
    peer = addr[0]
    print(f"client connected {peer}:{addr[1]}", flush=True)
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        while True:
            data = conn.recv(65536)
            if not data:
                break
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print(f"client disconnected {peer}", flush=True)

try:
    while True:
        c, a = sock.accept()
        threading.Thread(target=drain, args=(c, a), daemon=True).start()
except KeyboardInterrupt:
    print("\nstopped", flush=True)
finally:
    sock.close()
PY
}

if [[ "$STATUS_LISTEN" -eq 1 ]]; then
  run_status_listen
fi

if [[ "$WATCH" -eq 1 ]]; then
  echo "watching live TCP on :$PORT (Ctrl+C to stop)"
  echo
  while true; do
    clear 2>/dev/null || printf '\n----------\n'
    show_once || true
    sleep "$INTERVAL"
  done
else
  show_once
fi
