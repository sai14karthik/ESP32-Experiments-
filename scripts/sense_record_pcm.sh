#!/usr/bin/env bash
# Record continuous Sense PCM → WAV (all audio, no VAD drops).
#
#   ./scripts/sense_record_pcm.sh --ip 10.128.93.15
#   ./scripts/sense_record_pcm.sh --ip 10.128.93.15 -o /tmp/session.wav
#
# Then accurate ASR+diarization (WhisperX, prefer CUDA GPU):
#   ./scripts/sense_whisperx.sh /tmp/session.wav
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p recordings
exec uv run python firmware/tools/sense_record_pcm.py "$@"
