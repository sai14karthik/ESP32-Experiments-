#!/usr/bin/env bash
# Run CSI ingest on this Mac or Mac Mini (same command).
# Usage:
#   ./run_ingest.sh --method 4.3 --channel 11 --label desk
#   ./run_ingest.sh --port /dev/cu.usbmodem1101 --label desk
#   ./run_ingest.sh --from-file fixtures/sample_csi_lines.csv --label dryrun
#   ./run_ingest.sh --probe   # print which port is emitting CSI_DATA
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  # strip CR so Windows-edited .env does not break DATABASE_URL
  source <(tr -d '\r' < "$ROOT/.env")
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing .venv — run ./setup_mac.sh first." >&2
  exit 1
fi

if [[ "${1:-}" == "--probe" ]]; then
  shift
  exec "$PY" "$ROOT/probe_recv_port.py" "$@"
fi

# If no --port / --from-file, try auto-pick the CSI_DATA port when multiple exist.
has_port=0
has_file=0
for a in "$@"; do
  case "$a" in
    --port|--port=*) has_port=1 ;;
    --from-file|--from-file=*) has_file=1 ;;
  esac
done

EXTRA=()
if [[ $has_port -eq 0 && $has_file -eq 0 ]]; then
  RECV="$("$PY" "$ROOT/probe_recv_port.py" --quiet || true)"
  if [[ -n "${RECV:-}" ]]; then
    echo "auto recv port: $RECV"
    EXTRA=(--port "$RECV")
  fi
fi

# macOS bash 3.2 + set -u: empty "${EXTRA[@]}" is an unbound variable
if [[ ${#EXTRA[@]} -gt 0 ]]; then
  exec "$PY" "$ROOT/ingest_serial.py" "${EXTRA[@]}" "$@"
else
  exec "$PY" "$ROOT/ingest_serial.py" "$@"
fi
