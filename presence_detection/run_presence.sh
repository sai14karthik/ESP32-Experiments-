#!/usr/bin/env bash
# Presence detection front door (multi-RX CSI). Capture stays in csi_pipeline_new;
# models/exports live here under presence_detection/.
#
#   ./run_presence.sh capture empty_01      # ~2 min multi-ingest label
#   ./run_presence.sh clients               # how many C5s on :9055
#   ./run_presence.sh train                 # export empty+occupied → fuse train → sync models/
#   ./run_presence.sh calibrate             # empty-room cal from export (TCP path)
#   ./run_presence.sh live                  # TCP :9055 fused live (stop ingest first)
#   ./run_presence.sh web                   # live + phone UI on :8765
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
  ./run_presence.sh calibrate             # empty-room cal from training CSV
  ./run_presence.sh calibrate-live        # EMPTY room over TCP :9055 (fix live)
  ./run_presence.sh live                  # continuous scores (--fast)
  ./run_presence.sh live --quiet          # state changes only
  ./run_presence.sh gui                   # PyQt dashboard (TCP multi-RX)
  ./run_presence.sh web                   # phone web UI (http://<mini-ip>:8765)
  ./run_presence.sh web --http-port 8765  # optional port override
  ./run_presence.sh test-web              # unit/integration tests (no hardware)
  ./run_presence.sh test-e2e              # full A–Z pipeline proof (no hardware)
  ./run_presence.sh eval                  # print saved metrics
  ./run_presence.sh status                # model + calibration summary
  ./run_presence.sh sync                  # copy CSI models/exports → here
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

# Live/gui/calibrate-live need a usable presence model.
# - N≥2 train → rx_fusion=concat (multi-RX)
# - N=1 train → single-RX (rx_fusion empty) — still valid on :9055 with one board
require_presence_model() {
  local mp
  mp="$(resolve_model)"
  if [[ ! -f "$mp" ]]; then
    echo "No model — run ./run_presence.sh train first" >&2
    exit 2
  fi
  local fusion nsrc
  fusion="$(
    uv_csi -c "import joblib; print(joblib.load(r'''$mp''').get('rx_fusion') or '')" 2>/dev/null || true
  )"
  nsrc="$(
    uv_csi -c "import joblib; b=joblib.load(r'''$mp'''); print(len(b.get('rx_sources_order') or []))" 2>/dev/null || true
  )"
  if [[ "$fusion" == "concat" ]]; then
    if [[ ! -f "$MODELS/object_detector.joblib" ]]; then
      echo "WARNING: fused model is outside presence_detection/models/: $mp" >&2
    fi
    echo "$mp"
    return 0
  fi
  # Single-RX model (trained with 1 board) — OK for N=1 live.
  if [[ -z "$fusion" || "$fusion" == "none" || "$fusion" == "None" ]]; then
    echo "model: single-RX (N=1). Live works with one board; add boards → retrain." >&2
    echo "$mp"
    return 0
  fi
  echo "Unrecognized model fusion='$fusion' at $mp" >&2
  echo "  Fix: ./run_presence.sh capture … && ./run_presence.sh train" >&2
  exit 2
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
    # Match live: fault-tolerant partial RX sets (min 1 board per bin).
    if [[ ! " ${REST[*]-} " =~ " --rx-min " && ! " ${REST[*]-} " =~ " --rx-min=" ]]; then
      REST=(--rx-min 1 "${REST[@]+"${REST[@]}"}")
    fi
    "$CSI/run_detect.sh" --train-from-db "${INCLUDE[@]}" \
      --out "$MODELS/object_detector.joblib" "${REST[@]+"${REST[@]}"}"
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
    echo "Next (room EMPTY, stop ingest): ./run_presence.sh calibrate-live" >&2
    echo "Then: ./run_presence.sh live   # or: ./run_presence.sh gui / web" >&2
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
      CAL_EXTRA=(--fast "${CAL_EXTRA[@]+"${CAL_EXTRA[@]}"}")
    fi
    if [[ ! " $* " =~ " --fpr " && ! " $* " =~ " --fpr=" ]]; then
      CAL_EXTRA=(--fpr 0.05 "${CAL_EXTRA[@]+"${CAL_EXTRA[@]}"}")
    fi
    # strip our sentinel if present
    OUT_EXTRA=()
    for a in "${CAL_EXTRA[@]+"${CAL_EXTRA[@]}"}"; do
      [[ "$a" == "--no-fast" ]] && continue
      OUT_EXTRA+=("$a")
    done
    uv_csi "$CSI/calibrate_site.py" \
      --model "$MP" \
      --out "$MODELS/site_calibration.joblib" \
      --from-csv "$CSV" \
      "${OUT_EXTRA[@]+"${OUT_EXTRA[@]}"}"
    mkdir -p "$CSI/models"
    cp -f "$MODELS/site_calibration.joblib" "$CSI/models/site_calibration.joblib"
    echo "synced calibration → $MODELS/site_calibration.joblib" >&2
    echo "For live domain match prefer: ./run_presence.sh calibrate-live" >&2
    ;;
  calibrate-live)
    MP="$(require_presence_model)"
    echo "Leave the room EMPTY. Stop ingest/live first (port :9055)." >&2
    # Match live: --fast. Longer default so slow LabPSK rates still fuse.
    CAL_EXTRA=(--fast --fpr 0.05 --seconds 120)
    if [[ $# -gt 0 ]]; then
      CAL_EXTRA+=("$@")
    fi
    uv_csi "$CSI/calibrate_site.py" \
      --model "$MP" \
      --out "$MODELS/site_calibration.joblib" \
      --listen-tcp 9055 \
      "${CAL_EXTRA[@]}"
    mkdir -p "$CSI/models"
    cp -f "$MODELS/site_calibration.joblib" "$CSI/models/site_calibration.joblib"
    echo "synced calibration → $MODELS/site_calibration.joblib" >&2
    echo "Next: ./run_presence.sh live   # or: ./run_presence.sh gui / web" >&2
    ;;
  live)
    MP="$(require_presence_model)"
    CAL="$(resolve_cal)"
    # Continuous scores by default (--fast). Pass --quiet for state-change only.
    LIVE_ARGS=(--model "$MP" --listen-tcp 9055 --fast)
    if [[ -n "$CAL" ]]; then
      LIVE_ARGS+=(--calibration "$CAL")
    else
      echo "WARNING: no site_calibration.joblib — run ./run_presence.sh calibrate-live first" >&2
    fi
    echo "Stop ./run_multi_ingest.sh first if it holds :9055" >&2
    exec "$CSI/run_detect.sh" --skip-probe "${LIVE_ARGS[@]}" "$@"
    ;;
  gui)
    MP="$(require_presence_model)"
    CAL="$(resolve_cal)"
    GUI_ARGS=(--model "$MP" --listen-tcp 9055 --fast --gui)
    if [[ -n "$CAL" ]]; then
      GUI_ARGS+=(--calibration "$CAL")
    else
      echo "WARNING: no site_calibration.joblib — run ./run_presence.sh calibrate-live first" >&2
    fi
    echo "Stop ingest/terminal live first if they hold :9055" >&2
    exec "$CSI/run_detect.sh" --skip-probe "${GUI_ARGS[@]}" "$@"
    ;;
  web)
    MP="$(require_presence_model)"
    CAL="$(resolve_cal)"
    WEB_ARGS=(--model "$MP" --listen-tcp 9055 --fast --web --http-port 8765)
    if [[ -n "$CAL" ]]; then
      WEB_ARGS+=(--calibration "$CAL")
    else
      echo "WARNING: no site_calibration.joblib — run ./run_presence.sh calibrate-live first" >&2
    fi
    echo "Stop ingest/terminal live/gui first if they hold :9055" >&2
    echo "Phone on LabPSK: http://10.128.93.23:8765" >&2
    exec "$CSI/run_detect.sh" --skip-probe "${WEB_ARGS[@]}" "$@"
    ;;
  test-web)
    ensure_uv
    echo "Running presence web tests (no hardware)…" >&2
    uv_csi "$ROOT/tests/test_web_live.py" "$@"
    ;;
  test-e2e)
    ensure_uv
    echo "Running full presence E2E (no hardware)…" >&2
    uv_csi "$CSI/test_presence_e2e.py" "$@"
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
