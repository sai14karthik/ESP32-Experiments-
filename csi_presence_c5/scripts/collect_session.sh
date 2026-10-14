#!/usr/bin/env bash
# CSI collect + YOLO26-pose GT (aligned with healthcare_pose_activity).
#   ./scripts/collect_session.sh --rx /dev/cu.usbmodem1101 --camera 0
#   POSE_QUALITY=fast ./scripts/collect_session.sh ...
#   POSE_MODEL=yolo26l-pose.pt ./scripts/collect_session.sh ...
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  uv sync
fi
QUALITY="${POSE_QUALITY:-accurate}"
EXTRA=()
# Only inject --quality if caller did not already pass it
has_quality=0
for a in "$@"; do
  if [[ "$a" == "--quality" || "$a" == --quality=* ]]; then
    has_quality=1
    break
  fi
done
if [[ "$has_quality" -eq 0 ]]; then
  EXTRA+=(--quality "$QUALITY")
fi
if [[ -n "${POSE_MODEL:-}" ]]; then
  EXTRA+=(--model "$POSE_MODEL")
fi
exec uv run csi-collect "${EXTRA[@]}" "$@"
