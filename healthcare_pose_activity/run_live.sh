#!/usr/bin/env bash
# Live healthcare pose + activity (standing/sitting/walking/lying/fall_suspected).
# Prefer macOS Terminal.app for camera/display permission.
#
#   ./run_live.sh 0
#   ./run_live.sh rtsp://127.0.0.1:18554/cam_sense
#   ./run_live.sh http://10.128.93.17:81/stream
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
SOURCE="${1:-0}"
shift || true
export POSE_MODEL="${POSE_MODEL:-yolo11n-pose.pt}"
exec uv run --with ultralytics --with opencv-python --with numpy --with lap \
  python src/live_infer.py "$SOURCE" --model "$POSE_MODEL" "$@"
