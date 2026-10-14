#!/usr/bin/env bash
# Preview YOLO26-pose GT (same presets as healthcare_pose_activity).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  uv sync
fi
QUALITY="${POSE_QUALITY:-accurate}"
EXTRA=(--quality "$QUALITY")
if [[ -n "${POSE_MODEL:-}" ]]; then
  EXTRA+=(--model "$POSE_MODEL")
fi
exec uv run csi-cv-label "${EXTRA[@]}" "$@"
