#!/usr/bin/env bash
# On the Mini, with or without collect_to_mini. Does not bind the CSI ports.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
export PATH="/opt/homebrew/opt/postgresql@16/bin:${PATH:-}"
export DATABASE_URL="${DATABASE_URL:-postgresql:///csi}"
export PYTHONPATH="$REPO/csi_presence_c5/src${PYTHONPATH:+:$PYTHONPATH}"
export POSE_CONF="${POSE_CONF:-0.55}"
# Do not let FFmpeg hold seconds of RTSP video before the camera card sees it.
export OPENCV_FFMPEG_CAPTURE_OPTIONS="${OPENCV_FFMPEG_CAPTURE_OPTIONS:-rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0|probesize;32768|analyzeduration;0}"
PY="$REPO/csi_presence_c5/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing $PY" >&2
  echo "On the Mini: cd $REPO/csi_presence_c5 && uv sync" >&2
  exit 1
fi
exec "$PY" "$HERE/live_room.py" "$@"
