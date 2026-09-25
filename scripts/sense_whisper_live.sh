#!/usr/bin/env bash
# Live Whisper + speaker labels from Sense mic — one command for every mode.
#
#   export HF_TOKEN=…   # accept pyannote/speaker-diarization-community-1
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.15
#
# Same script covers: live talk, YouTube, one or two speakers.
# Optional WAV seeds (better lock): --enroll-you me.wav --enroll-other them.wav
# Optional hard force: --expect-speakers 2   (usually unnecessary)
#
# Boards:
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.34
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.25,10.128.93.34
#   ./scripts/sense_whisper_live.sh --ip all
#
# Fallback enroll-only matcher: --diarize-backend ecapa
# Full-file WhisperX: sense_record_pcm.sh + sense_whisperx.sh
#
# Default (no args) = 10.128.93.34
# PCM ports follow IP (.25→19055, .34→19056) — restart mediamtx after pull.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
