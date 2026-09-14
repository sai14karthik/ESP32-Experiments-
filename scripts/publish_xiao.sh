#!/usr/bin/env bash
# Bridge XIAO CameraWebServerWiFi into MediaMTX path cam_xiao.
#
# Default mode = capture (recommended):
#   Poll http://<ip>/capture stills and encode — avoids ESP MJPEG /stream
#   dying around ~60–90s (HLS "network timeout" / forever loading).
#
# Stream mode (legacy, flaky on XIAO):
#   PUBLISH_MODE=stream ./scripts/publish_xiao.sh http://<ip>:81/stream
#
# Terminal A: ./scripts/mediamtx_run.sh
# Terminal B: ./scripts/publish_xiao.sh http://10.128.93.25:81/stream
#   (URL may be :81/stream; capture URL is derived as http://<host>/capture)
set -uo pipefail

MTX_URL="${MTX_URL:-rtsp://127.0.0.1:8554/cam_xiao}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
MODE="${PUBLISH_MODE:-capture}"   # capture | stream
FPS="${XIAO_FPS:-6}"
BITRATE="${XIAO_BITRATE:-400k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-15}"
# Proactive reconnect before the typical ~70s ESP /stream death.
MAX_LIFE_S="${PUBLISH_MAX_LIFE_S:-45}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found. Install: brew install ffmpeg" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found" >&2
  exit 1
fi

if [[ -z "$XIAO_URL" ]]; then
  cat >&2 <<'EOF'
Usage:
  ./scripts/publish_xiao.sh http://<xiao-ip>:81/stream

Capture mode (default) uses http://<xiao-ip>/capture
Stream mode: PUBLISH_MODE=stream ./scripts/publish_xiao.sh http://<xiao-ip>:81/stream
EOF
  exit 2
fi

# http://10.128.93.25:81/stream → http://10.128.93.25/capture
capture_url_from() {
  local u="$1"
  u="${u%%/stream}"
  u="${u%:81}"
  u="${u%/}"
  echo "${u}/capture"
}

CAPTURE_URL="$(capture_url_from "$XIAO_URL")"

echo "Bridging XIAO → MediaMTX (mode=$MODE, stall=${STALL_S}s, maxlife=${MAX_LIFE_S}s)" >&2
echo "  stream : $XIAO_URL" >&2
echo "  capture: $CAPTURE_URL" >&2
echo "  dest   : $MTX_URL  fps=$FPS bitrate=$BITRATE" >&2
echo "Ctrl+C to stop. Do not open ESP /stream in a browser while publishing." >&2

kill_pid_tree() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  kill "$pid" 2>/dev/null || true
  sleep 0.3
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

watch_progress() {
  local progress="$1" fpid="$2" started="$3"
  local last="" last_change=$SECONDS out now
  while kill -0 "$fpid" 2>/dev/null; do
    if [[ -f "$progress" ]]; then
      out="$(grep -E '^out_time_ms=' "$progress" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
      if [[ -n "$out" && "$out" != "$last" ]]; then
        last="$out"
        last_change=$SECONDS
      fi
    fi
    now=$SECONDS
    if (( now - last_change >= STALL_S )); then
      echo "$(date '+%H:%M:%S') stall ${STALL_S}s — killing bridge" >&2
      return 1
    fi
    if (( now - started >= MAX_LIFE_S )); then
      echo "$(date '+%H:%M:%S') max life ${MAX_LIFE_S}s — refreshing ESP link" >&2
      return 1
    fi
    sleep 1
  done
  return 0
}

run_capture() {
  local progress fpid feeder started
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  started=$SECONDS

  # Continuous still polls — each JPEG is a fresh HTTP GET (stable on ESP).
  (
    while true; do
      if curl -fsS --max-time 3 "$CAPTURE_URL"; then
        :
      else
        sleep 0.4
      fi
      # Pace ~FPS (capture is slower than stream; 6 fps is enough for lab).
      sleep "$(awk -v f="$FPS" 'BEGIN{printf "%.3f", 1/f}')"
    done
  ) | ffmpeg -hide_banner -loglevel error \
      -nostats \
      -progress "$progress" \
      -fflags +genpts+discardcorrupt \
      -f image2pipe \
      -framerate "$FPS" \
      -c:v mjpeg \
      -i - \
      -an \
      -c:v libx264 \
      -preset veryfast \
      -tune zerolatency \
      -pix_fmt yuv420p \
      -b:v "$BITRATE" \
      -maxrate "$BITRATE" \
      -bufsize "$BITRATE" \
      -g $((FPS * 2)) \
      -bf 0 \
      -f rtsp \
      -rtsp_transport tcp \
      "$MTX_URL" &
  fpid=$!
  # shellcheck disable=SC2064
  trap 'rm -f "$progress"; kill_pid_tree "$fpid"' RETURN

  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pid_tree "$fpid"
    return 1
  fi
  wait "$fpid" 2>/dev/null || true
}

run_stream() {
  local progress fpid started
  progress="$(mktemp -t xiao_str_XXXXXX)"
  started=$SECONDS

  ffmpeg -hide_banner -loglevel error \
    -nostats \
    -progress "$progress" \
    -xerror \
    -fflags +nobuffer+genpts+discardcorrupt \
    -flags low_delay \
    -rw_timeout 8000000 \
    -f mjpeg \
    -use_wallclock_as_timestamps 1 \
    -r "$FPS" \
    -i "$XIAO_URL" \
    -an \
    -c:v libx264 \
    -preset veryfast \
    -tune zerolatency \
    -pix_fmt yuv420p \
    -b:v "$BITRATE" \
    -maxrate "$BITRATE" \
    -bufsize "$BITRATE" \
    -g $((FPS * 2)) \
    -bf 0 \
    -f rtsp \
    -rtsp_transport tcp \
    "$MTX_URL" &
  fpid=$!
  trap 'rm -f "$progress"; kill_pid_tree "$fpid"' RETURN

  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pid_tree "$fpid"
    return 1
  fi
  wait "$fpid" 2>/dev/null || true
}

run_once() {
  if [[ "$MODE" == "stream" ]]; then
    run_stream
  else
    run_capture
  fi
}

# Quick sanity: can Mini reach capture?
if [[ "$MODE" == "capture" ]]; then
  if ! curl -fsS --max-time 3 -o /dev/null "$CAPTURE_URL"; then
    echo "WARN: $CAPTURE_URL not reachable from Mini — check ESP / LabPSK" >&2
  else
    echo "capture OK: $CAPTURE_URL" >&2
  fi
fi

if [[ "${PUBLISH_ONCE:-0}" == "1" ]]; then
  run_once
  exit $?
fi

while true; do
  run_once || true
  echo "$(date '+%H:%M:%S') bridge restart in ${RETRY_S}s…" >&2
  sleep "$RETRY_S"
done
