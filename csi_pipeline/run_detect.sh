#!/usr/bin/env bash
# Real-time CSI object detection on Mac / Mac Mini.
#
#   ./run_detect.sh                          # live serial
#   ./run_detect.sh --train                  # (re)train best model from sample CSV
#   ./run_detect.sh --from-file fixtures/sample_csi_lines.csv
#   ./run_detect.sh --quiet                  # print only on EMPTY ↔ OBJECT change
#   ./run_detect.sh --probe                  # same as run_ingest.sh --probe
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

ML_PY="$ROOT/.venv-ml/bin/python"
INGEST_PY="$ROOT/.venv/bin/python"

if [[ "${1:-}" == "--probe" ]]; then
  if [[ ! -x "$INGEST_PY" ]]; then
    echo "Missing .venv — run ./setup_mac.sh first." >&2
    exit 1
  fi
  exec "$INGEST_PY" "$ROOT/probe_recv_port.py" "${@:2}"
fi

if [[ "${1:-}" == "--train" ]]; then
  shift
  if [[ ! -x "$ML_PY" ]]; then
    python3 -m venv "$ROOT/.venv-ml"
    "$ROOT/.venv-ml/bin/pip" install -q -r "$ROOT/requirements-ml.txt"
  fi
  exec "$ML_PY" "$ROOT/train_object_detector.py" --deploy "$@"
fi

if [[ "${1:-}" == "--eval-csv" ]]; then
  shift
  [[ $# -gt 0 && "${1:-}" != --* ]] && shift || true
  exec "$ML_PY" "$ROOT/eval_object_detector.py" --model "$ROOT/models/object_detector.joblib" "$@"
fi

if [[ ! -x "$ML_PY" ]]; then
  echo "ML venv missing. Run: ./run_detect.sh --train" >&2
  exit 1
fi

if [[ ! -f "$ROOT/models/object_detector.joblib" ]]; then
  echo "No model yet — training from sample data …" >&2
  "$ML_PY" "$ROOT/train_object_detector.py" --deploy
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

if [[ $has_port -eq 0 && $has_file -eq 0 && -x "$INGEST_PY" ]]; then
  RECV="$("$INGEST_PY" "$ROOT/probe_recv_port.py" --quiet 2>/dev/null || true)"
  if [[ -n "${RECV:-}" ]]; then
    echo "auto recv port: $RECV" >&2
    EXTRA=(--port "$RECV")
  fi
fi

if [[ ${#EXTRA[@]} -gt 0 ]]; then
  exec "$ML_PY" "$ROOT/detect_live.py" "${EXTRA[@]}" "$@"
else
  exec "$ML_PY" "$ROOT/detect_live.py" "$@"
fi
