#!/usr/bin/env bash
# Mac Mini collect: CSI TCP :9055 + YOLO26-pose GT from ESP cam .15
#   ./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense
#   denser: hop 0.25s (~4 windows/s), default 300s — override with --seconds / --hop-sec
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  uv sync
fi
QUALITY="${POSE_QUALITY:-accurate}"
# Stricter than healthcare live (0.25) — empty-room chairs were false "occupied"
PERSON_CONF="${POSE_CONF:-0.55}"
EXTRA=()
has_quality=0
has_person_conf=0
for a in "$@"; do
  if [[ "$a" == "--quality" || "$a" == --quality=* ]]; then
    has_quality=1
  fi
  if [[ "$a" == "--person-conf" || "$a" == --person-conf=* ]]; then
    has_person_conf=1
  fi
done
if [[ "$has_quality" -eq 0 ]]; then
  EXTRA+=(--quality "$QUALITY")
fi
if [[ "$has_person_conf" -eq 0 ]]; then
  EXTRA+=(--person-conf "$PERSON_CONF")
fi
if [[ -n "${POSE_MODEL:-}" ]]; then
  EXTRA+=(--model "$POSE_MODEL")
fi
if [[ -n "${CSI_GT_CAMERA:-}" ]]; then
  EXTRA+=(--camera "$CSI_GT_CAMERA")
fi
exec uv run csi-collect "${EXTRA[@]}" "$@"
