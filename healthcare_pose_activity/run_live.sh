#!/usr/bin/env bash
# Live healthcare pose + activity (standing/sitting/walking/lying/fall_suspected).
# Prefer macOS Terminal.app for camera/display permission.
#
#   ./run_live.sh 0
#   ./run_live.sh rtsp://127.0.0.1:8554/cam_sense
#   POSE_QUALITY=fast ./run_live.sh 0
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
SOURCE="${1:-0}"
shift || true

export POSE_QUALITY="${POSE_QUALITY:-accurate}"
export OPENCV_FFMPEG_CAPTURE_OPTIONS="${OPENCV_FFMPEG_CAPTURE_OPTIONS:-rtsp_transport;tcp}"

ARGS=(--quality "$POSE_QUALITY")
if [[ -n "${POSE_MODEL:-}" ]]; then
  ARGS+=(--model "$POSE_MODEL")
fi

VENV="${ROOT}/.venv"
DEPS=("ultralytics>=8.4.175" opencv-python numpy lap)

# Persistent venv — avoids hitting PyPI every launch (lab DNS often broken)
ensure_venv() {
  if [[ -x "${VENV}/bin/python" ]]; then
    return 0
  fi
  echo "Creating .venv (needs DNS/PyPI once)…" >&2
  uv venv "${VENV}"
  uv pip install --python "${VENV}/bin/python" "${DEPS[@]}"
}

# If venv exists → offline. Else try offline cache, then online install.
if [[ -x "${VENV}/bin/python" ]]; then
  exec "${VENV}/bin/python" src/live_infer.py "$SOURCE" "${ARGS[@]}" "$@"
fi

if UV_OFFLINE=1 uv run --with ultralytics --with opencv-python --with numpy --with lap \
  python src/live_infer.py "$SOURCE" "${ARGS[@]}" "$@" 2>/dev/null; then
  exit 0
fi

echo "PyPI/DNS needed to install deps once. Fix DNS then re-run." >&2
echo "  System Settings → Network → Ethernet → DNS → add 8.8.8.8 and 1.1.1.1" >&2
echo "  or: sudo networksetup -setdnsservers Ethernet 8.8.8.8 1.1.1.1" >&2
ensure_venv
exec "${VENV}/bin/python" src/live_infer.py "$SOURCE" "${ARGS[@]}" "$@"
