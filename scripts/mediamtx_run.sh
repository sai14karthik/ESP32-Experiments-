#!/usr/bin/env bash
# MediaMTX + ffmpeg: N× ESP HTTP MJPEG → H.264 RTSP paths cam_xiao, cam_xiao2, …
#
#   ./scripts/mediamtx_run.sh
#   ./scripts/mediamtx_run.sh http://10.128.93.25:81/stream http://10.128.93.34:81/stream
#   XIAO_MJPEG_URLS=http://a:81/stream,http://b:81/stream ./scripts/mediamtx_run.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
HOST_LAN="${MEDIAMTX_LAN_IP:-10.128.93.23}"

# Collect MJPEG URLs: CLI args > XIAO_MJPEG_URLS > defaults (lab two boards).
URLS=()
if [[ $# -gt 0 ]]; then
  URLS=("$@")
elif [[ -n "${XIAO_MJPEG_URLS:-}" ]]; then
  IFS=',' read -r -a URLS <<<"$XIAO_MJPEG_URLS"
elif [[ -n "${XIAO_MJPEG_URL:-}" || -n "${XIAO2_MJPEG_URL:-}" ]]; then
  [[ -n "${XIAO_MJPEG_URL:-}" ]] && URLS+=("$XIAO_MJPEG_URL")
  [[ -n "${XIAO2_MJPEG_URL:-}" ]] && URLS+=("$XIAO2_MJPEG_URL")
else
  URLS=(
    "http://10.128.93.25:81/stream"
    "http://10.128.93.34:81/stream"
  )
fi

if [[ ${#URLS[@]} -lt 1 ]]; then
  echo "Need ≥1 http:// ESP MJPEG URL (:81/stream)." >&2
  exit 2
fi

for u in "${URLS[@]}"; do
  u="${u#"${u%%[![:space:]]*}"}"
  u="${u%"${u##*[![:space:]]}"}"
  if [[ "$u" != http://* ]]; then
    echo "Need http:// ESP MJPEG. Got: $u" >&2
    exit 2
  fi
done

if ! command -v mediamtx >/dev/null 2>&1; then
  echo "Install: brew install mediamtx" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Install: brew install ffmpeg" >&2
  exit 1
fi
if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 8554 in use. Run: pkill -f mediamtx" >&2
  exit 1
fi

path_name() {
  local i="$1"
  if [[ "$i" -eq 1 ]]; then
    echo "cam_xiao"
  else
    echo "cam_xiao${i}"
  fi
}

# Emit one MediaMTX path block (YAML).
emit_path() {
  local name="$1" url="$2"
  cat <<EOF
  ${name}:
    source: publisher
    runOnInit: >-
      ffmpeg -hide_banner -loglevel warning
      -fflags nobuffer+genpts+discardcorrupt
      -flags low_delay
      -probesize 256k
      -analyzeduration 0
      -use_wallclock_as_timestamps 1
      -f mjpeg
      -i ${url}
      -an
      -vf fps=12,format=yuv420p
      -c:v libx264
      -preset veryfast
      -tune zerolatency
      -profile:v high
      -level 4.0
      -pix_fmt yuv420p
      -bf 0
      -g 12
      -keyint_min 12
      -crf 20
      -maxrate 2500k
      -bufsize 1250k
      -x264-params scenecut=0:repeat-headers=1:nal-hrd=cbr
      -flush_packets 1
      -muxdelay 0
      -muxpreload 0
      -f rtsp
      -rtsp_transport tcp
      rtsp://127.0.0.1:\$RTSP_PORT/\$MTX_PATH
    runOnInitRestart: yes
EOF
}

# Base config without empty paths:{} — replace with generated paths.
{
  grep -v '^paths:' "$CONF_SRC" | grep -v '^$'
  echo
  echo "paths:"
  i=1
  for u in "${URLS[@]}"; do
    u="${u#"${u%%[![:space:]]*}"}"
    u="${u%"${u##*[![:space:]]}"}"
    name="$(path_name "$i")"
    emit_path "$name" "$u"
    i=$((i + 1))
  done
} >"$CONF_RT"

echo "N=${#URLS[@]} camera(s)" >&2
i=1
for u in "${URLS[@]}"; do
  u="${u#"${u%%[![:space:]]*}"}"
  u="${u%"${u##*[![:space:]]}"}"
  name="$(path_name "$i")"
  echo "  [$i] $u" >&2
  echo "      rtsp://127.0.0.1:8554/${name}" >&2
  echo "      rtsp://${HOST_LAN}:8554/${name}" >&2
  echo "      http://${HOST_LAN}:8888/${name}/" >&2
  i=$((i + 1))
done

exec mediamtx "$CONF_RT"
