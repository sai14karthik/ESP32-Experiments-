#!/usr/bin/env bash
# Live Whisper + speaker labels from Sense mic.
#
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.15
#
# HF token — do ONCE on this machine (no export every time):
#   uv run --group whisper hf auth login
#   # accept: https://huggingface.co/pyannote/speaker-diarization-community-1
# Or put HF_TOKEN=… in repo-root .env (gitignored).
#
# Captions only by default. Extra logs: --verbose
# Optional seeds: --enroll-you me.wav --enroll-other them.wav
#
# Boards: --ip 10.128.93.34 | --ip all
# Default (no args) = 10.128.93.34
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env once if present (HF_TOKEN=hf_… etc.)
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# Reuse huggingface-cli / hf login cache when env not set
if [[ -z "${HF_TOKEN:-}${HUGGINGFACE_HUB_TOKEN:-}${HF_HUB_TOKEN:-}" ]]; then
  if tok="$(uv run --group whisper hf auth token 2>/dev/null)" && [[ -n "$tok" ]]; then
    export HF_TOKEN="$tok"
  elif tok="$(uv run --group whisper huggingface-cli whoami -t 2>/dev/null | awk '/^Token:/{print $2}')" && [[ -n "$tok" ]]; then
    export HF_TOKEN="$tok"
  fi
fi

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
