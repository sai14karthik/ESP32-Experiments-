#!/usr/bin/env bash
# Live healthcare pose + activity (standing/sitting/walking/lying/fall_suspected).
# Prefer macOS Terminal.app for camera/display permission.
#
#   ./run_live.sh 0
#   ./run_live.sh rtsp://127.0.0.1:18554/cam_sense
#   POSE_MODEL=yolo26n-pose.pt ./run_live.sh 0   # faster, less accurate
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
SOURCE="${1:-0}"
shift || true
# Default: YOLO26s-pose (more accurate keypoints). Faster: POSE_MODEL=yolo26n-pose.pt
export POSE_MODEL="${POSE_MODEL:-yolo26s-pose.pt}"
exec uv run --with "ultralytics>=8.4.175" --with opencv-python --with numpy --with lap \
  python src/live_infer.py "$SOURCE" --model "$POSE_MODEL" "$@"
