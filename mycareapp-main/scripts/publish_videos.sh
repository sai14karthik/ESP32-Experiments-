#!/usr/bin/env bash
set -euo pipefail
RTSP_HOST="${RTSP_HOST:-127.0.0.1}"
RTSP_PORT="${RTSP_PORT:-8554}"
VIDEOS_DIR="${VIDEOS_DIR:-/rpm/videos}"
STREAM_PREFIX="${STREAM_PREFIX:-test}"

pids=()
stream_index=0

for video in $(find "$VIDEOS_DIR" -maxdepth 1 -type f \
    \( -name '*.mp4' -o -name '*.mkv' -o -name '*.avi' -o -name '*.mov' \) \
    | sort); do
    stream_index=$((stream_index + 1))
    rtsp_url="rtsp://${RTSP_HOST}:${RTSP_PORT}/${STREAM_PREFIX}/video${stream_index}"
    echo "Publishing ${video} -> ${rtsp_url}"
    ffmpeg \
        -loglevel warning \
        -re \
        -stream_loop -1 \
        -i "$video" \
        -c:v libx264 \
        -preset veryfast \
        -tune zerolatency \
        -g 30 \
        -keyint_min 30 \
        -sc_threshold 0 \
        -x264-params "repeat-headers=1" \
        -an \
        -f rtsp \
        -rtsp_transport tcp \
        "$rtsp_url" &
    pids+=("$!")
done

if [ ${#pids[@]} -eq 0 ]; then
    echo "No video files found in ${VIDEOS_DIR}" >&2
    exit 1
fi

echo "Running ${#pids[@]} stream(s)..."
exit_code=0
for pid in "${pids[@]}"; do
    wait "$pid" || exit_code=1
done
exit "$exit_code"
