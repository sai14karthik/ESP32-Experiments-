#!/usr/bin/env bash
# Real-time CSI object detection (uv + csi dependency group).
#
#   ./run_detect.sh                          # live serial (probes CSI first)
#   ./run_detect.sh --gui                    # live PyQt presence window (EMPTY/PRESENCE)
#   ./run_detect.sh --gui --fast             # GUI + low-latency updates
#   ./run_detect.sh --calibrate              # USB recv, or empty rows of training CSV if no USB
#   ./run_detect.sh --calibrate --from-csv exports/training_packets.csv  # TCP multi-RX path
#   ./run_detect.sh --train                  # train from default sample CSV
#   ./run_detect.sh --train-from-db          # export Postgres → train
#   ./run_detect.sh --train-from-db --include baseline_desk,object_desk
#   ./run_detect.sh --eval-csv               # print saved hold-out metrics
#   ./run_detect.sh --listen-tcp             # multi-RX live on :9055 (fused models; stop ingest first)
#   ./run_detect.sh --fast --quiet           # recommended live (auto TCP if model is fused)
#   ./run_detect.sh --probe                  # which USB port has CSI_DATA
#   ./run_detect.sh --diagnose               # recv port + model checklist
#   ./run_detect.sh --diagnose --all-ports   # also probe usbserial (resets sender)
#   ./run_detect.sh --self-test              # software checks (+ hardware if linked)
#   ./run_detect.sh --ablate                 # which feature blocks carry the signal
#   ./run_detect.sh --skip-probe             # live without CSI pre-check (not recommended)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT/uv_common.sh"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/bin:$PATH"

if [[ "${1:-}" == "--probe" ]]; then
  shift
  uv_csi "$ROOT/probe_recv_port.py" --all-ports "$@"
  exit 0
fi

if [[ "${1:-}" == "--diagnose" ]]; then
  shift
  uv_csi "$ROOT/diagnose_csi.py" "$@"
  exit 0
fi

if [[ "${1:-}" == "--self-test" ]]; then
  shift
  uv_csi "$ROOT/self_test.py" "$@"
  exit 0
fi

if [[ "${1:-}" == "--ablate" ]]; then
  shift
  uv_csi "$ROOT/ablate.py" "$@"
  exit 0
fi

# Calibration: USB serial OR empty rows from a multi-RX training CSV (TCP path).
# Wall-powered C5s have no USB — use --from-csv / auto-fallback below.
if [[ "${1:-}" == "--calibrate" ]]; then
  shift
  CAL_ARGS=("$@")
  if [[ ! " $* " =~ " --port " && ! " $* " =~ " --from-csv " && ! " $* " =~ " --from-file " ]]; then
    if RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet --seconds 8 2>/dev/null)"; then
      echo "auto recv port: $RECV" >&2
      CAL_ARGS=(--port "$RECV" "$@")
    elif [[ -f "$ROOT/exports/training_packets.csv" ]]; then
      echo "No USB CSI — calibrating from empty rows of exports/training_packets.csv (TCP multi-RX)." >&2
      CAL_ARGS=(--from-csv "$ROOT/exports/training_packets.csv" "$@")
    else
      echo "No CSI_DATA on recv port (usbmodem*) and no exports/training_packets.csv." >&2
      echo "  TCP path: ./run_detect.sh --calibrate --from-csv exports/training_packets.csv" >&2
      echo "  Or re-export: ./run_detect.sh --train-from-db --include empty,occupied" >&2
      echo "  Run ./run_detect.sh --diagnose" >&2
      exit 2
    fi
  fi
  uv_csi "$ROOT/calibrate_site.py" "${CAL_ARGS[@]}"
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
  EXPORT_ARGS=()
  TRAIN_ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      # Space-separated form: the value is the next argv element, so take both.
      --include|--exclude|--source-id)
        EXPORT_ARGS+=("$1")
        shift
        if [[ $# -gt 0 ]]; then
          EXPORT_ARGS+=("$1")
          shift
        fi
        ;;
      --include=*|--exclude=*|--source-id=*)
        EXPORT_ARGS+=("$1")
        shift
        ;;
      *)
        TRAIN_ARGS+=("$1")
        shift
        ;;
    esac
  done
  if [[ ${#EXPORT_ARGS[@]} -gt 0 ]]; then
    uv_csi "$ROOT/export_training_csv.py" --out "$EXPORT" "${EXPORT_ARGS[@]}"
  else
    uv_csi "$ROOT/export_training_csv.py" --out "$EXPORT"
  fi
  if [[ ${#TRAIN_ARGS[@]} -gt 0 ]]; then
    uv_csi "$ROOT/train_object_detector.py" --deploy --csv "$EXPORT" "${TRAIN_ARGS[@]}"
  else
    uv_csi "$ROOT/train_object_detector.py" --deploy --csv "$EXPORT"
  fi
  exit 0
fi

if [[ "${1:-}" == "--eval-csv" ]]; then
  shift
  uv_csi "$ROOT/eval_object_detector.py" --model "$ROOT/models/object_detector.joblib" "$@"
  exit 0
fi

# Resolve --model path for fusion check / skip sample auto-train when explicit.
MODEL_PATH="$ROOT/models/object_detector.joblib"
prev=""
for a in "$@"; do
  if [[ "$prev" == "--model" ]]; then
    MODEL_PATH="$a"
    break
  fi
  case "$a" in
    --model=*) MODEL_PATH="${a#--model=}"; break ;;
  esac
  prev="$a"
done

has_explicit_model=0
for a in "$@"; do
  case "$a" in
    --model|--model=*) has_explicit_model=1; break ;;
  esac
done

if [[ ! -f "$MODEL_PATH" ]]; then
  if [[ $has_explicit_model -eq 1 ]]; then
    echo "Model not found: $MODEL_PATH" >&2
    echo "  Train: cd ../presence_detection && ./run_presence.sh train" >&2
    exit 2
  fi
  if [[ ! -f "$ROOT/models/object_detector.joblib" ]]; then
    echo "No model yet — training from sample data …" >&2
    uv_csi "$ROOT/train_object_detector.py" --deploy
  fi
  MODEL_PATH="$ROOT/models/object_detector.joblib"
fi

EXTRA=()
DETECT_ARGS=()
has_port=0
has_file=0
has_listen_tcp=0
skip_probe=0
use_gui=0
# Preserve user flags for detect_live; only --skip-probe is wrapper-only.
prev=""
for a in "$@"; do
  case "$a" in
    --skip-probe) skip_probe=1; prev=""; continue ;;
    --gui) use_gui=1; DETECT_ARGS+=("$a"); prev=""; continue ;;
    --port|--port=*) has_port=1 ;;
    --from-file|--from-file=*) has_file=1 ;;
    --listen-tcp|--listen-tcp=*) has_listen_tcp=1 ;;
  esac
  DETECT_ARGS+=("$a")
  prev="$a"
done

# Fused multi-RX models need TCP fan-in, not USB serial.
if [[ $has_port -eq 0 && $has_file -eq 0 && $has_listen_tcp -eq 0 ]]; then
  FUSION="$(
    uv_csi -c "import joblib; b=joblib.load(r'''${MODEL_PATH}'''); print(b.get('rx_fusion') or '')" 2>/dev/null || true
  )"
  if [[ "$FUSION" == "concat" ]]; then
    echo "multi-RX fused model — TCP :9055 (stop ./run_multi_ingest.sh if it holds the port)" >&2
    EXTRA=(--listen-tcp 9055)
    has_listen_tcp=1
  fi
fi

if [[ $has_port -eq 0 && $has_file -eq 0 && $has_listen_tcp -eq 0 ]]; then
  if [[ $skip_probe -eq 0 ]]; then
    if ! RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet --seconds 8 2>/dev/null)"; then
      echo "No CSI_DATA on recv port (usbmodem*)." >&2
      echo "  • Fused multi-RX: ./run_detect.sh --listen-tcp --fast --quiet" >&2
      echo "  • Or: cd ../presence_detection && ./run_presence.sh live" >&2
      echo "  • Recv → USB Mac (/dev/cu.usbmodem*)" >&2
      echo "  • Skip check: ./run_detect.sh --skip-probe" >&2
      exit 2
    fi
    echo "auto recv port: $RECV" >&2
    EXTRA=(--port "$RECV")
  else
    RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet --seconds 2 2>/dev/null || true)"
    if [[ -n "${RECV:-}" ]]; then
      echo "auto recv port: $RECV" >&2
      EXTRA=(--port "$RECV")
    else
      echo "warning: --skip-probe and no CSI seen; using first USB port" >&2
    fi
  fi
fi

DETECT_SCRIPT="$ROOT/detect_live.py"
if [[ $use_gui -eq 1 ]]; then
  DETECT_SCRIPT="$ROOT/detect_gui.py"
fi

if [[ ${#EXTRA[@]} -gt 0 && ${#DETECT_ARGS[@]} -gt 0 ]]; then
  uv_csi "$DETECT_SCRIPT" "${EXTRA[@]}" "${DETECT_ARGS[@]}"
elif [[ ${#EXTRA[@]} -gt 0 ]]; then
  uv_csi "$DETECT_SCRIPT" "${EXTRA[@]}"
elif [[ ${#DETECT_ARGS[@]} -gt 0 ]]; then
  uv_csi "$DETECT_SCRIPT" "${DETECT_ARGS[@]}"
else
  uv_csi "$DETECT_SCRIPT"
fi
