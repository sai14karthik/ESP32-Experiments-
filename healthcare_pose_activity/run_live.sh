#!/usr/bin/env bash
# Live healthcare pose + activity (standing/sitting/walking/lying/fall_suspected).
# Prefer macOS Terminal.app for camera/display permission.
#
#   ./run_live.sh 0                          # accurate (YOLO26m-pose, default)
#   POSE_QUALITY=fast ./run_live.sh 0        # YOLO26n — max FPS
#   POSE_QUALITY=balanced ./run_live.sh 0    # YOLO26s
#   POSE_QUALITY=max ./run_live.sh 0         # YOLO26l — highest mAP, slower
#   ./run_live.sh rtsp://127.0.0.1:18554/cam_sense
#   ./run_live.sh /path/to/clip.mp4          # loop a file; live RTSP path is unchanged
#
# Lab laptop path (Nemotron is on by default through the same SSH session):
#   Mini:  SENSE_AV_URL=http://10.128.93.15 ./scripts/mediamtx_run.sh
#   Laptop tunnel, leave it open:
#     ssh -N -L 18554:10.128.93.13:8554 -L 18000:10.43.41.152:8000 ojl1977@10.128.81.10
#   Laptop: ./run_live.sh rtsp://127.0.0.1:18554/cam_sense
#   --no-nemotron turns the vision read off. Press n to ask, q to quit.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
SOURCE="${1:-0}"
shift || true

# accurate → yolo26m-pose (68.8 COCO pose mAP) — best live accuracy/speed on Mac
export POSE_QUALITY="${POSE_QUALITY:-accurate}"
# Nemotron reads a new fall, or the current frame when you press n.
# 127.0.0.1:18000 is the laptop end of the SSH forward to the cluster vLLM service.
export POSE_NEMOTRON="${POSE_NEMOTRON:-1}"
export NEMOTRON_ENDPOINT="${NEMOTRON_ENDPOINT:-http://127.0.0.1:18000/v1}"
# MediaMTX / ESP RTSP: force TCP (stops Ultralytics "Waiting for stream 0")
export OPENCV_FFMPEG_CAPTURE_OPTIONS="${OPENCV_FFMPEG_CAPTURE_OPTIONS:-rtsp_transport;tcp}"

# Optional hard override: POSE_MODEL=yolo26l-pose.pt
ARGS=(--quality "$POSE_QUALITY")
if [[ -n "${POSE_MODEL:-}" ]]; then
  ARGS+=(--model "$POSE_MODEL")
fi

exec uv run --no-project --with "ultralytics>=8.4.175" --with opencv-python --with numpy --with lap \
  python src/live_infer.py "$SOURCE" "${ARGS[@]}" "$@"
