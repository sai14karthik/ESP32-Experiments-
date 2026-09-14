#!/usr/bin/env bash
# Bridge XIAO CameraWebServerWiFi into MediaMTX path cam_xiao.
#
# Why not "forever" on one TCP?
#   MediaMTX can run forever (runOnInitRestart). ESP /stream MJPEG over Wi‑Fi
#   typically dies ~60–90s — that is the camera HTTP stack, not MediaMTX.
#   Default = poll /capture stills (fresh GET each frame) + restart on stall.
#
# Preferred (one terminal — MediaMTX restarts this script):
#   ./scripts/mediamtx_run.sh
#
# Manual loop:
#   ./scripts/publish_xiao.sh http://10.128.93.25:81/stream
#
# Modes:
#   PUBLISH_MODE=capture  (default, stable)
#   PUBLISH_MODE=stream   (legacy :81/stream — flaky)
#   PUBLISH_ONCE=1        (one session then exit — used by MediaMTX runOnInit)
set -uo pipefail

# MediaMTX runOnInit sets RTSP_PORT + MTX_PATH (official hook env).
MTX_URL="${MTX_URL:-rtsp://127.0.0.1:${RTSP_PORT:-8554}/${MTX_PATH:-cam_xiao}}"
XIAO_URL="${1:-${XIAO_MJPEG_URL:-}}"
MODE="${PUBLISH_MODE:-capture}"
FPS="${XIAO_FPS:-6}"
BITRATE="${XIAO_BITRATE:-400k}"
RETRY_S="${PUBLISH_RETRY_S:-2}"
STALL_S="${PUBLISH_STALL_S:-20}"
if [[ -z "${PUBLISH_MAX_LIFE_S:-}" ]]; then
  MAX_LIFE_S=0
else
  MAX_LIFE_S="${PUBLISH_MAX_LIFE_S}"
fi

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

Prefer: ./scripts/mediamtx_run.sh   # MediaMTX owns forever-restart
EOF
  exit 2
fi

capture_url_from() {
  local u="$1"
  u="${u%%/stream}"
  u="${u%:81}"
  u="${u%/}"
  echo "${u}/capture"
}

CAPTURE_URL="$(capture_url_from "$XIAO_URL")"

echo "Bridging XIAO → MediaMTX (mode=$MODE stall=${STALL_S}s once=${PUBLISH_ONCE:-0})" >&2
echo "  capture: $CAPTURE_URL" >&2
echo "  dest   : $MTX_URL  fps=$FPS bitrate=$BITRATE" >&2

kill_pgid() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  # Kill whole pipeline process group (curl feeder + ffmpeg).
  kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  sleep 0.3
  kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
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
    if (( STALL_S > 0 && now - last_change >= STALL_S )); then
      echo "$(date '+%H:%M:%S') stall ${STALL_S}s — exit so MediaMTX/loop can restart" >&2
      return 1
    fi
    if (( MAX_LIFE_S > 0 && now - started >= MAX_LIFE_S )); then
      echo "$(date '+%H:%M:%S') max life ${MAX_LIFE_S}s — refreshing" >&2
      return 1
    fi
    sleep 1
  done
  return 0
}

run_capture() {
  local progress fpid started
  progress="$(mktemp -t xiao_cap_XXXXXX)"
  started=$SECONDS
  set -m
  (
    while true; do
      curl -fsS --max-time 3 "$CAPTURE_URL" || sleep 0.5
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
      -profile:v baseline \
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
  trap 'rm -f "$progress"; kill_pgid "$fpid"' RETURN

  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pgid "$fpid"
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
    -profile:v baseline \
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
  trap 'rm -f "$progress"; kill_pgid "$fpid"' RETURN

  if ! watch_progress "$progress" "$fpid" "$started"; then
    kill_pgid "$fpid"
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

if [[ "$MODE" == "capture" ]]; then
  if curl -fsS --max-time 3 -o /dev/null "$CAPTURE_URL"; then
    echo "capture OK: $CAPTURE_URL" >&2
  else
    echo "WARN: $CAPTURE_URL not reachable" >&2
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
