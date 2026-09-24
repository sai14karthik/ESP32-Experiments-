#!/usr/bin/env bash
# MediaMTX + ffmpeg:
#   N× video-only MJPEG → cam_xiao, cam_xiao2, …
#   N× Sense A/V (MJPEG + PCM) → cam_sense, cam_sense2, …
#
#   ./scripts/mediamtx_run.sh
#   ./scripts/mediamtx_run.sh http://10.128.93.25:81/stream http://10.128.93.34:81/stream
#   SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.40 ./scripts/mediamtx_run.sh
#   SENSE_AV_URL=http://10.128.93.40 ./scripts/mediamtx_run.sh   # single (alias)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONF_SRC="$ROOT/mediamtx/mediamtx.yml"
CONF_RT="$ROOT/mediamtx/mediamtx.runtime.yml"
HOST_LAN="${MEDIAMTX_LAN_IP:-10.128.93.13}"

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

# Collect Sense A/V bases: SENSE_AV_URLS (comma) and/or SENSE_AV_URL (single).
SENSE_BASES=()
if [[ -n "${SENSE_AV_URLS:-}" ]]; then
  IFS=',' read -r -a _sense_raw <<<"$SENSE_AV_URLS"
  for u in "${_sense_raw[@]}"; do
    b="$(normalize_sense_base "$u")"
    [[ -n "$b" ]] && SENSE_BASES+=("$b")
  done
fi
if [[ -n "${SENSE_AV_URL:-}" ]]; then
  SENSE_BASES+=("$(normalize_sense_base "$SENSE_AV_URL")")
fi

# Collect MJPEG URLs: CLI args > XIAO_MJPEG_URLS > defaults (lab two boards).
# If any Sense A/V is set and no CLI/URLS env, skip video-only defaults.
URLS=()
if [[ $# -gt 0 ]]; then
  URLS=("$@")
elif [[ -n "${XIAO_MJPEG_URLS:-}" ]]; then
  IFS=',' read -r -a URLS <<<"$XIAO_MJPEG_URLS"
elif [[ -n "${XIAO_MJPEG_URL:-}" || -n "${XIAO2_MJPEG_URL:-}" ]]; then
  [[ -n "${XIAO_MJPEG_URL:-}" ]] && URLS+=("$XIAO_MJPEG_URL")
  [[ -n "${XIAO2_MJPEG_URL:-}" ]] && URLS+=("$XIAO2_MJPEG_URL")
elif [[ ${#SENSE_BASES[@]} -eq 0 ]]; then
  URLS=(
    "http://10.128.93.25:81/stream"
    "http://10.128.93.34:81/stream"
  )
fi

if [[ ${#URLS[@]} -lt 1 && ${#SENSE_BASES[@]} -lt 1 ]]; then
  echo "Need ≥1 MJPEG URL and/or SENSE_AV_URLS=http://ip1,http://ip2,…" >&2
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

for b in "${SENSE_BASES[@]+"${SENSE_BASES[@]}"}"; do
  if [[ "$b" != http://* ]]; then
    echo "Sense A/V URL must be http://<esp-ip> (got: $b)" >&2
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

sense_path_name() {
  local i="$1"
  if [[ "$i" -eq 1 ]]; then
    echo "cam_sense"
  else
    echo "cam_sense${i}"
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

# Sense A/V: wrapper script (avoids wallclock lag when muxing MJPEG+PCM).
emit_av_path() {
  local name="$1" base="$2"
  local helper="$ROOT/scripts/ffmpeg_sense_av.sh"
  cat <<EOF
  ${name}:
    source: publisher
    runOnInit: ${helper} ${base}
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
  i=1
  for b in "${SENSE_BASES[@]+"${SENSE_BASES[@]}"}"; do
    name="$(sense_path_name "$i")"
    emit_av_path "$name" "$b"
    i=$((i + 1))
  done
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

echo "N=${#SENSE_BASES[@]} Sense A/V camera(s)" >&2
i=1
for b in "${SENSE_BASES[@]+"${SENSE_BASES[@]}"}"; do
  name="$(sense_path_name "$i")"
  echo "  [$i] ${b} → ${name}" >&2
  echo "      video ${b}:81/stream" >&2
  echo "      audio ${b}/audio" >&2
  echo "      rtsp://127.0.0.1:8554/${name}" >&2
  echo "      rtsp://${HOST_LAN}:8554/${name}" >&2
  echo "      http://${HOST_LAN}:8888/${name}/" >&2
  i=$((i + 1))
done

exec mediamtx "$CONF_RT"
