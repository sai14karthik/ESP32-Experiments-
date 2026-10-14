#!/usr/bin/env bash
# Mac Mini collect: CSI TCP :9055 + YOLO26-pose GT from ESP cam .15
#   ./scripts/collect_session.sh --seconds 120
#   ./scripts/collect_session.sh --camera http://10.128.93.15:81/stream
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  uv sync
fi
QUALITY="${POSE_QUALITY:-accurate}"
EXTRA=()
has_quality=0
for a in "$@"; do
  if [[ "$a" == "--quality" || "$a" == --quality=* ]]; then
    has_quality=1
  fi
done
if [[ "$has_quality" -eq 0 ]]; then
  EXTRA+=(--quality "$QUALITY")
fi
if [[ -n "${POSE_MODEL:-}" ]]; then
  EXTRA+=(--model "$POSE_MODEL")
fi
if [[ -n "${CSI_GT_CAMERA:-}" ]]; then
  EXTRA+=(--camera "$CSI_GT_CAMERA")
fi
exec uv run csi-collect "${EXTRA[@]}" "$@"
