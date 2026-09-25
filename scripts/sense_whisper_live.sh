#!/usr/bin/env bash
# Live Whisper from Sense mic — pick board by IP.
#
# MediaMTX can run both cameras; Whisper listens only where you point --ip:
#
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.34
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.25
#   ./scripts/sense_whisper_live.sh --ip 10.128.93.25,10.128.93.34
#   ./scripts/sense_whisper_live.sh --ip all
#
# Speakers (YOU vs OTHER) — ECAPA (best on Mini Metal):
#   Best accuracy — clean WAVs from the same Sense mic:
#     ./scripts/sense_whisper_live.sh --ip 10.128.93.15 \
#       --enroll-you ~/enroll_you.wav --enroll-other ~/enroll_other.wav
#   Live enroll — averages ~8s speech (several phrases), not a single 3s clip:
#     ./scripts/sense_whisper_live.sh --ip 10.128.93.15 --diarize-backend ecapa
#
# Offline NeMo/whisper-diarization (recorded WAV, CUDA): ./scripts/sense_diarize_offline.sh
#
# Default (no args) = 10.128.93.34
# PCM ports follow IP (.25→19055, .34→19056) — restart mediamtx after pull.
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec uv run --group whisper python firmware/tools/sense_whisper_live.py "$@"
