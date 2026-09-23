#!/usr/bin/env bash
# Live Whisper from Sense PCM — Apple Metal via mlx-whisper (default).
#
# Default: listen on ALL Sense PCM tees (19055–19058) so wall or collar
# captions work no matter which board is cam_sense vs cam_sense2.
#
#   SENSE_AV_URLS=http://10.128.93.25,http://10.128.93.34 ./scripts/mediamtx_run.sh
#   ./scripts/sense_whisper_live.sh
#
# Optional: --pcm-udp 19056  (single board only)
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Strip legacy aliases (--collar / --wall); default auto covers both.
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --collar|--wall|--desk)
      # no-op: multi-port auto is the default
      shift
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
