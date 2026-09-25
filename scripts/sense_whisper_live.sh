#!/usr/bin/env bash
# Live Whisper + speaker labels from Sense mic.
#
#   export HF_TOKEN=…
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.15
#
# Captions only by default. Extra logs: --verbose
# Optional seeds: --enroll-you me.wav --enroll-other them.wav
#
# Boards: --ip 10.128.93.34 | --ip 10.128.93.25,10.128.93.34 | --ip all
# Default (no args) = 10.128.93.34
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
