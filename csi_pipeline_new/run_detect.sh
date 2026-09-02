#!/usr/bin/env bash
# Real-time CSI object detection (uv + csi dependency group).
#
#   ./run_detect.sh                          # live serial (probes CSI first)
#   ./run_detect.sh --calibrate              # record the empty room here, set baseline+threshold
#   ./run_detect.sh --train                  # train from default sample CSV
#   ./run_detect.sh --train-from-db          # export Postgres → train
#   ./run_detect.sh --train-from-db --include baseline_desk,object_desk
#   ./run_detect.sh --eval-csv               # print saved hold-out metrics
#   ./run_detect.sh --quiet                  # live: print only on EMPTY ↔ OBJECT
#   ./run_detect.sh --fast                   # low-latency: stride=1, no EMA (~0.2s updates)
#   ./run_detect.sh --fast --quiet           # fast + quiet (recommended live)
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

# Calibration needs the recv board, so find it the same way live detection does
# rather than letting calibrate_site.py fall back to the first USB port — which
# on this rig is often the sender.
if [[ "${1:-}" == "--calibrate" ]]; then
  shift
  CAL_ARGS=("$@")
  if [[ ! " $* " =~ " --port " && ! " $* " =~ " --from-csv " && ! " $* " =~ " --from-file " ]]; then
    if ! RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet --seconds 8 2>/dev/null)"; then
      echo "No CSI_DATA on recv port (usbmodem*). Cannot calibrate." >&2
      echo "  Run ./run_detect.sh --diagnose" >&2
      exit 2
    fi
    echo "auto recv port: $RECV" >&2
    CAL_ARGS=(--port "$RECV" "$@")
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
      --include|--exclude)
        EXPORT_ARGS+=("$1")
        shift
        if [[ $# -gt 0 ]]; then
          EXPORT_ARGS+=("$1")
          shift
        fi
        ;;
      --include=*|--exclude=*)
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

if [[ ! -f "$ROOT/models/object_detector.joblib" ]]; then
  echo "No model yet — training from sample data …" >&2
  uv_csi "$ROOT/train_object_detector.py" --deploy
fi

EXTRA=()
DETECT_ARGS=()
has_port=0
has_file=0
skip_probe=0
for a in "$@"; do
  case "$a" in
    --port|--port=*) has_port=1 ;;
    --from-file|--from-file=*) has_file=1 ;;
    --skip-probe) skip_probe=1 ;;
    *) DETECT_ARGS+=("$a") ;;
  esac
done

if [[ $has_port -eq 0 && $has_file -eq 0 ]]; then
  if [[ $skip_probe -eq 0 ]]; then
    if ! RECV="$(uv_csi "$ROOT/probe_recv_port.py" --quiet --seconds 8 2>/dev/null)"; then
      echo "No CSI_DATA on recv port (usbmodem*)." >&2
      echo "  • Recv → USB Mac (/dev/cu.usbmodem*)" >&2
      echo "  • Send → powered (not USB data), within ~2 m, channel 11" >&2
      echo "  • Reset both boards, then: ./run_detect.sh --diagnose" >&2
      echo "  • Reflash: cd .. && ./scripts/flash_csi_pair.sh /dev/cu.usbserial-10 /dev/cu.usbmodem2101" >&2
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

if [[ ${#EXTRA[@]} -gt 0 && ${#DETECT_ARGS[@]} -gt 0 ]]; then
  uv_csi "$ROOT/detect_live.py" "${EXTRA[@]}" "${DETECT_ARGS[@]}"
elif [[ ${#EXTRA[@]} -gt 0 ]]; then
  uv_csi "$ROOT/detect_live.py" "${EXTRA[@]}"
elif [[ ${#DETECT_ARGS[@]} -gt 0 ]]; then
  uv_csi "$ROOT/detect_live.py" "${DETECT_ARGS[@]}"
else
  uv_csi "$ROOT/detect_live.py"
fi
