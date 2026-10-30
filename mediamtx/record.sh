#!/usr/bin/env bash
# Save a MediaMTX stream to an MP4 file using ffmpeg.
#
#   ./mediamtx/record.sh
#   ./mediamtx/record.sh cam_sense2
#   ./mediamtx/record.sh cam_sense -o recordings/sense-demo.mp4 -t 120
#
# Needs ffmpeg. The default saves cam_sense for 120 seconds.
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

ARGS=(ffmpeg -hide_banner -rtsp_transport tcp -i "$URL")
if [[ -n "$DURATION" && "$DURATION" != "0" ]]; then
  ARGS+=(-t "$DURATION")
fi
ARGS+=(-map 0:v:0 -map '0:a:0?' -c copy)
if [[ ${#EXTRA[@]} -gt 0 ]]; then
  ARGS+=("${EXTRA[@]}")
fi
mkdir -p "$(dirname "$OUTPUT")"
ARGS+=("$OUTPUT")

exec "${ARGS[@]}"
