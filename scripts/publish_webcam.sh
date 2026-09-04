#!/usr/bin/env bash
# Publish this Mac's webcam into MediaMTX path cam1 (RTSP).
#
# Prerequisites:
#   Terminal A:  ./scripts/mediamtx_run.sh
#   Terminal B:  ./scripts/publish_webcam.sh
# Watch:
#   VLC:     open rtsp://127.0.0.1:8554/cam1
#   Browser: http://127.0.0.1:8888/cam1/  (HLS) or http://127.0.0.1:8889/cam1/ (WebRTC)
set -euo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:8554/cam1}"
DEVICE="${WEBCAM_DEVICE:-}"
SIZE="${WEBCAM_SIZE:-1280x720}"
FPS="${WEBCAM_FPS:-30}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi

if [[ -z "$DEVICE" ]]; then
  echo "Discovering AVFoundation video devices…" >&2
  # ffmpeg lists devices on stderr and exits non-zero; don't fail the script.
  LIST="$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 || true)"
  echo "$LIST" | sed -n '/AVFoundation video devices/,/AVFoundation audio devices/p' >&2
  # Prefer FaceTime / built-in camera index if present, else first video device [0].
  if echo "$LIST" | grep -qi 'FaceTime'; then
    DEVICE="$(echo "$LIST" | grep -i 'FaceTime' | head -1 | sed -n 's/.*\[\([0-9][0-9]*\)\].*/\1/p')"
  fi
  DEVICE="${DEVICE:-0}"
  echo "Using video device index: $DEVICE  (override with WEBCAM_DEVICE=N)" >&2
fi

echo "Publishing webcam → $MTX_URL" >&2
echo "Device=$DEVICE size=$SIZE fps=$FPS  Ctrl+C to stop." >&2

# macOS: video:audio — ":none" skips audio for a simpler lab stream.
exec ffmpeg -hide_banner -loglevel info \
  -f avfoundation \
  -framerate "$FPS" \
  -video_size "$SIZE" \
  -i "${DEVICE}:none" \
  -an \
  -c:v libx264 \
  -preset veryfast \
  -tune zerolatency \
  -pix_fmt yuv420p \
  -g $((FPS * 2)) \
  -f rtsp \
  -rtsp_transport tcp \
  "$MTX_URL"
