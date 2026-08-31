#!/usr/bin/env bash
# Real-time CSI object detection (uv + csi dependency group).
#
#   ./run_detect.sh                          # live serial
#   ./run_detect.sh --train                  # train from default sample CSV
#   ./run_detect.sh --train-from-db          # export Postgres → train
#   ./run_detect.sh --eval-csv               # print saved hold-out metrics
#   ./run_detect.sh --quiet                  # live: print only on EMPTY ↔ OBJECT
#   ./run_detect.sh --probe                  # which USB port has CSI_DATA
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT/uv_common.sh"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

if [[ "${1:-}" == "--probe" ]]; then
  shift
  uv_csi "$ROOT/probe_recv_port.py" "$@"
  exit 0
fi

if [[ "${1:-}" == "--train" ]]; then
  shift
  uv_csi "$ROOT/train_object_detector.py" --deploy "$@"
  exit 0
fi

if [[ "${1:-}" == "--train-from-db" ]]; then
  shift
  EXPORT="$ROOT/exports/training_packets.csv"
  uv_csi "$ROOT/export_training_csv.py" --out "$EXPORT"
  uv_csi "$ROOT/train_object_detector.py" --deploy --csv "$EXPORT" "$@"
  exit 0
fi

if [[ "${1:-}" == "--eval-csv" ]]; then
  shift
  [[ $# -gt 0 && "${1:-}" != --* ]] && shift || true
  uv_csi "$ROOT/eval_object_detector.py" --model "$ROOT/models/object_detector.joblib" "$@"
  exit 0
fi

if [[ ! -f "$ROOT/models/object_detector.joblib" ]]; then
  echo "No model yet — training from sample data …" >&2
  uv_csi "$ROOT/train_object_detector.py" --deploy
fi

EXTRA=()
has_port=0
has_file=0
for a in "$@"; do
  case "$a" in
    --port|--port=*) has_port=1 ;;
    --from-file|--from-file=*) has_file=1 ;;
  esac
done

if [[ $has_port -eq 0 && $has_file -eq 0 ]]; then
  RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet 2>/dev/null || true)"
  if [[ -n "${RECV:-}" ]]; then
    echo "auto recv port: $RECV" >&2
    EXTRA=(--port "$RECV")
  fi
fi

if [[ ${#EXTRA[@]} -gt 0 ]]; then
  uv_csi "$ROOT/detect_live.py" "${EXTRA[@]}" "$@"
else
  uv_csi "$ROOT/detect_live.py" "$@"
fi
