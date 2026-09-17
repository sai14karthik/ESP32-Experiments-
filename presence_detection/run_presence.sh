#!/usr/bin/env bash
# Presence detection front door (multi-RX CSI). Capture stays in csi_pipeline_new;
# models/exports live here under presence_detection/.
#
#   ./run_presence.sh capture empty_01      # ~2 min multi-ingest label
#   ./run_presence.sh clients               # how many C5s on :9055
#   ./run_presence.sh train                 # export empty+occupied → fuse train → sync models/
#   ./run_presence.sh calibrate             # empty-room cal from export (TCP path)
#   ./run_presence.sh live                  # TCP :9055 fused live (stop ingest first)
#   ./run_presence.sh eval                  # print saved metrics
#   ./run_presence.sh status                # model + calibration summary
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
CSI="$REPO/csi_pipeline_new"
MODELS="$ROOT/models"
EXPORTS="$ROOT/exports"

mkdir -p "$MODELS" "$EXPORTS"

# shellcheck disable=SC1091
source "$CSI/uv_common.sh"

usage() {
  cat <<'EOF'
Presence detection front door (multi-RX CSI).

  ./run_presence.sh capture empty_01      # ~2 min multi-ingest label
  ./run_presence.sh clients               # how many C5s on :9055
  ./run_presence.sh train                 # export empty+occupied → fuse train → models/
  ./run_presence.sh calibrate             # empty-room cal from export (TCP path)
  ./run_presence.sh live                  # TCP :9055 fused live (stop ingest first)
  ./run_presence.sh eval                  # print saved metrics
  ./run_presence.sh status                # model + calibration summary
  ./run_presence.sh sync                  # copy CSI models/exports → presence_detection/
EOF
  exit "${1:-0}"
}

resolve_model() {
  if [[ -f "$MODELS/object_detector.joblib" ]]; then
    echo "$MODELS/object_detector.joblib"
  else
    echo "$CSI/models/object_detector.joblib"
  fi
}

resolve_cal() {
  if [[ -f "$MODELS/site_calibration.joblib" ]]; then
    echo "$MODELS/site_calibration.joblib"
  elif [[ -f "$CSI/models/site_calibration.joblib" ]]; then
    echo "$CSI/models/site_calibration.joblib"
  else
    echo ""
  fi
}

resolve_csv() {
  if [[ -f "$EXPORTS/training_packets.csv" ]]; then
    echo "$EXPORTS/training_packets.csv"
  else
    echo "$CSI/exports/training_packets.csv"
  fi
}

sync_from_csi() {
  if [[ -f "$CSI/models/object_detector.joblib" ]]; then
    cp -f "$CSI/models/object_detector.joblib" "$MODELS/object_detector.joblib"
    echo "synced model → $MODELS/object_detector.joblib" >&2
  fi
  if [[ -f "$CSI/models/site_calibration.joblib" ]]; then
    cp -f "$CSI/models/site_calibration.joblib" "$MODELS/site_calibration.joblib"
    echo "synced calibration → $MODELS/site_calibration.joblib" >&2
  fi
  if [[ -f "$CSI/exports/training_packets.csv" ]]; then
    cp -f "$CSI/exports/training_packets.csv" "$EXPORTS/training_packets.csv"
    echo "synced export → $EXPORTS/training_packets.csv" >&2
  fi
}

cmd="${1:-}"
shift || true

case "$cmd" in
  -h|--help|help|"")
    usage 0
    ;;
  capture)
    label="${1:-}"
    if [[ -z "$label" ]]; then
      echo "usage: ./run_presence.sh capture <label>   e.g. empty_01 or occupied_01" >&2
      exit 2
    fi
    shift || true
    exec "$CSI/run_multi_ingest.sh" --label "$label" "$@"
    ;;
  clients|count)
    exec "$CSI/count_csi_clients.sh" "$@"
    ;;
  train)
    INCLUDE=()
    REST=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --include|--include=*)
          INCLUDE+=("$1")
          if [[ "$1" == "--include" && $# -gt 1 ]]; then
            INCLUDE+=("$2")
            shift
          fi
          shift
          ;;
        *)
          REST+=("$1")
          shift
          ;;
      esac
    done
    if [[ ${#INCLUDE[@]} -eq 0 ]]; then
      INCLUDE=(--include empty,occupied)
    fi
    "$CSI/run_detect.sh" --train-from-db "${INCLUDE[@]}" \
      --out "$MODELS/object_detector.joblib" "${REST[@]}"
    # train-from-db writes via train_object_detector --out; also refresh CSV copy
    if [[ -f "$CSI/exports/training_packets.csv" ]]; then
      cp -f "$CSI/exports/training_packets.csv" "$EXPORTS/training_packets.csv"
      echo "synced export → $EXPORTS/training_packets.csv" >&2
    fi
    # If --out was honored under presence models, also mirror into csi for legacy scripts
    if [[ -f "$MODELS/object_detector.joblib" ]]; then
      mkdir -p "$CSI/models"
      cp -f "$MODELS/object_detector.joblib" "$CSI/models/object_detector.joblib"
      echo "mirrored model → $CSI/models/object_detector.joblib" >&2
    else
      sync_from_csi
    fi
    echo >&2
    echo "Next: ./run_presence.sh calibrate && ./run_presence.sh live" >&2
    ;;
  calibrate)
    CSV="$(resolve_csv)"
    MP="$(resolve_model)"
    if [[ ! -f "$CSV" ]]; then
      echo "No training CSV — run ./run_presence.sh train first" >&2
      exit 2
    fi
    if [[ ! -f "$MP" ]]; then
      echo "No model — run ./run_presence.sh train first" >&2
      exit 2
    fi
    # Default --fast to match ./run_presence.sh live (same EMA / FPR).
    CAL_EXTRA=("$@")
    if [[ ! " $* " =~ " --fast " && ! " $* " =~ " --no-fast " ]]; then
      CAL_EXTRA=(--fast "${CAL_EXTRA[@]}")
    fi
    # strip our sentinel if present
    OUT_EXTRA=()
    for a in "${CAL_EXTRA[@]}"; do
      [[ "$a" == "--no-fast" ]] && continue
      OUT_EXTRA+=("$a")
    done
    uv_csi "$CSI/calibrate_site.py" \
      --model "$MP" \
      --out "$MODELS/site_calibration.joblib" \
      --from-csv "$CSV" \
      "${OUT_EXTRA[@]}"
    mkdir -p "$CSI/models"
    cp -f "$MODELS/site_calibration.joblib" "$CSI/models/site_calibration.joblib"
    echo "synced calibration → $MODELS/site_calibration.joblib" >&2
    ;;
  live)
    MP="$(resolve_model)"
    CAL="$(resolve_cal)"
    if [[ ! -f "$MP" ]]; then
      echo "No model — run ./run_presence.sh train first" >&2
      exit 2
    fi
    # Continuous scores by default (--fast). Pass --quiet for state-change only.
    LIVE_ARGS=(--model "$MP" --listen-tcp 9055 --fast)
    if [[ -n "$CAL" ]]; then
      LIVE_ARGS+=(--calibration "$CAL")
    fi
    echo "Stop ./run_multi_ingest.sh first if it holds :9055" >&2
    exec "$CSI/run_detect.sh" --skip-probe "${LIVE_ARGS[@]}" "$@"
    ;;
  eval)
    MP="$(resolve_model)"
    if [[ ! -f "$MP" ]]; then
      echo "No model — run ./run_presence.sh train first" >&2
      exit 2
    fi
    uv_csi "$CSI/eval_object_detector.py" --model "$MP" "$@"
    ;;
  status)
    ensure_uv
    (cd "$REPO" && uv run --project "$REPO" --group csi python -m presence_detection.src.status "$@")
    ;;
  sync)
    sync_from_csi
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage 2
    ;;
esac
