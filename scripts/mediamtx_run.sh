#!/usr/bin/env bash
# MediaMTX + ffmpeg: N× ESP HTTP MJPEG → H.264 RTSP (cam_xiao…)
# Optional Sense A/V: SENSE_AV_URL=http://<ip> → cam_sense (MJPEG + PCM → H.264+AAC)
#
#   ./scripts/mediamtx_run.sh
#   ./scripts/mediamtx_run.sh http://10.128.93.25:81/stream http://10.128.93.34:81/stream
#   SENSE_AV_URL=http://10.128.93.40 ./scripts/mediamtx_run.sh
#   SENSE_AV_URL=http://10.128.93.40 ./scripts/mediamtx_run.sh \
#     http://10.128.93.25:81/stream http://10.128.93.34:81/stream
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
HOST_LAN="${MEDIAMTX_LAN_IP:-10.128.93.23}"

# Collect MJPEG URLs: CLI args > XIAO_MJPEG_URLS > defaults (lab two boards).
# If only SENSE_AV_URL is set, video-only defaults are skipped.
URLS=()
if [[ $# -gt 0 ]]; then
  URLS=("$@")
elif [[ -n "${XIAO_MJPEG_URLS:-}" ]]; then
  IFS=',' read -r -a URLS <<<"$XIAO_MJPEG_URLS"
elif [[ -n "${XIAO_MJPEG_URL:-}" || -n "${XIAO2_MJPEG_URL:-}" ]]; then
  [[ -n "${XIAO_MJPEG_URL:-}" ]] && URLS+=("$XIAO_MJPEG_URL")
  [[ -n "${XIAO2_MJPEG_URL:-}" ]] && URLS+=("$XIAO2_MJPEG_URL")
elif [[ -z "${SENSE_AV_URL:-}" ]]; then
  URLS=(
    "http://10.128.93.25:81/stream"
    "http://10.128.93.34:81/stream"
  )
fi

normalize_sense_base() {
  # Accept http://IP, http://IP/, http://IP:81/stream → http://IP
  local u="$1"
  u="${u#"${u%%[![:space:]]*}"}"
  u="${u%"${u##*[![:space:]]}"}"
  u="${u%/}"
  u="${u%/stream}"
  u="${u%:81}"
  echo "$u"
}

SENSE_BASE=""
if [[ -n "${SENSE_AV_URL:-}" ]]; then
  SENSE_BASE="$(normalize_sense_base "$SENSE_AV_URL")"
  if [[ "$SENSE_BASE" != http://* ]]; then
    echo "SENSE_AV_URL must be http://<esp-ip> (got: $SENSE_AV_URL)" >&2
    exit 2
  fi
fi

if [[ ${#URLS[@]} -lt 1 && -z "$SENSE_BASE" ]]; then
  echo "Need ≥1 MJPEG URL and/or SENSE_AV_URL=http://<ip>." >&2
  exit 2
fi

for u in "${URLS[@]+"${URLS[@]}"}"; do
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

# Emit one MediaMTX path block (video-only YAML).
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

# Sense A/V: MJPEG :81/stream + PCM :80/audio → H.264 + AAC RTSP cam_sense
emit_av_path() {
  local name="$1" base="$2"
  local vurl="${base}:81/stream"
  local aurl="${base}/audio"
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
      -i ${vurl}
      -f s16le -ar 16000 -ac 1
      -i ${aurl}
      -map 0:v:0 -map 1:a:0
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
      -c:a aac
      -b:a 64k
      -ar 16000
      -ac 1
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
  for u in "${URLS[@]+"${URLS[@]}"}"; do
    u="${u#"${u%%[![:space:]]*}"}"
    u="${u%"${u##*[![:space:]]}"}"
    name="$(path_name "$i")"
    emit_path "$name" "$u"
    i=$((i + 1))
  done
  if [[ -n "$SENSE_BASE" ]]; then
    emit_av_path "cam_sense" "$SENSE_BASE"
  fi
} >"$CONF_RT"

echo "N=${#URLS[@]} video-only camera(s)" >&2
i=1
for u in "${URLS[@]+"${URLS[@]}"}"; do
  u="${u#"${u%%[![:space:]]*}"}"
  u="${u%"${u##*[![:space:]]}"}"
  name="$(path_name "$i")"
  echo "  [$i] $u" >&2
  echo "      rtsp://127.0.0.1:8554/${name}" >&2
  echo "      rtsp://${HOST_LAN}:8554/${name}" >&2
  echo "      http://${HOST_LAN}:8888/${name}/" >&2
  i=$((i + 1))
done

if [[ -n "$SENSE_BASE" ]]; then
  echo "Sense A/V → cam_sense" >&2
  echo "  video ${SENSE_BASE}:81/stream" >&2
  echo "  audio ${SENSE_BASE}/audio" >&2
  echo "      rtsp://127.0.0.1:8554/cam_sense" >&2
  echo "      rtsp://${HOST_LAN}:8554/cam_sense" >&2
  echo "      http://${HOST_LAN}:8888/cam_sense/" >&2
fi

exec mediamtx "$CONF_RT"
