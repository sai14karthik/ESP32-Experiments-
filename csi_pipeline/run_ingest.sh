#!/usr/bin/env bash
# Run CSI ingest on this Mac or Mac Mini (uv + csi dependency group).
# Usage:
#   ./run_ingest.sh --method 4.3 --channel 11 --label desk
#   ./run_ingest.sh --port /dev/cu.usbmodem1101 --label desk
#   ./run_ingest.sh --from-file fixtures/sample_csi_lines.csv --label dryrun
#   ./run_ingest.sh --probe
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT/uv_common.sh"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$ROOT/.env")
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"

if [[ "${1:-}" == "--probe" ]]; then
  shift
  uv_csi "$ROOT/probe_recv_port.py" "$@"
  exit 0
fi

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
  RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet 2>/dev/null || true)"
  if [[ -n "${RECV:-}" ]]; then
    echo "auto recv port: $RECV"
    EXTRA=(--port "$RECV")
  fi
fi

if [[ ${#EXTRA[@]} -gt 0 ]]; then
  uv_csi "$ROOT/ingest_serial.py" "${EXTRA[@]}" "$@"
else
  uv_csi "$ROOT/ingest_serial.py" "$@"
fi
