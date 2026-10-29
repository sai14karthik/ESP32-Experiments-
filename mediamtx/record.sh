#!/usr/bin/env bash
# Record one MediaMTX path with video and audio.
#
#   ./mediamtx/record.sh
#   ./mediamtx/record.sh cam_sense2
#   ./mediamtx/record.sh cam_sense -o recordings/sense-demo.mp4 -t 120
#
# Default is cam_sense for 120 seconds. Ctrl-C finishes a playable MP4.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${MEDIAMTX_LAN_IP:-10.128.93.13}"
PATH_NAME="cam_sense"

if [[ $# -gt 0 && "$1" != -* ]]; then
  PATH_NAME="$1"
  shift
fi

if [[ "$PATH_NAME" == rtsp://* || "$PATH_NAME" == rtsps://* ]]; then
  URL="$PATH_NAME"
  STEM="rtsp"
else
  URL="rtsp://${HOST}:8554/${PATH_NAME}"
  STEM="$PATH_NAME"
fi

DURATION="120"
OUTPUT=""
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--duration)
      DURATION="${2:?duration seconds}"
      shift 2
      ;;
    -o|--output)
      OUTPUT="${2:?output file}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      EXTRA+=("$1")
      shift
      ;;
  esac
done

if [[ -z "$OUTPUT" ]]; then
  OUTPUT="${ROOT}/recordings/${STEM}-$(date +%Y%m%d-%H%M%S).mp4"
elif [[ "$OUTPUT" != /* ]]; then
  OUTPUT="${ROOT}/${OUTPUT}"
fi

ARGS=(python3 "${ROOT}/scripts/record_rtsp.py" "$URL" -o "$OUTPUT")
if [[ -n "$DURATION" && "$DURATION" != "0" ]]; then
  ARGS+=(-t "$DURATION")
fi
if [[ ${#EXTRA[@]} -gt 0 ]]; then
  ARGS+=("${EXTRA[@]}")
fi

exec "${ARGS[@]}"
