#!/usr/bin/env bash
# Accurate offline ASR + speaker diarization via WhisperX.
#
# Pipeline (batch file — NOT live Mini captions):
#   WAV → faster-whisper → wav2vec2 alignment → pyannote diarize → word speakers
#
# Prefer a CUDA GPU node (lab 10.128.81.x). On Mac Mini falls back to CPU (slow).
#
# Prereqs:
#   pip install whisperx   # or: uvx whisperx …
#   export HF_TOKEN=…      # accept pyannote/speaker-diarization-community-1
#
# Usage:
#   ./scripts/sense_record_pcm.sh --ip 10.128.93.15 -o session.wav   # capture
#   ./scripts/sense_whisperx.sh session.wav                          # accurate
#   ./scripts/sense_whisperx.sh session.wav --model large-v3 --language en
#
# Live captions stay on Mini: ./scripts/sense_whisper_live.sh (ECAPA, approximate)
#
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 AUDIO.wav [--model large-v3] [extra whisperx args…]" >&2
  exit 1
fi

AUDIO="$1"
shift
if [[ ! -f "$AUDIO" ]]; then
  echo "missing audio file: $AUDIO" >&2
  exit 1
fi

MODEL="large-v3"
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

DEVICE="cpu"
COMPUTE="int8"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  DEVICE="cuda"
  COMPUTE="float16"
fi

HF_ARGS=()
if [[ -n "${HF_TOKEN:-${HUGGINGFACE_HUB_TOKEN:-}}" ]]; then
  HF_ARGS=(--hf_token "${HF_TOKEN:-$HUGGINGFACE_HUB_TOKEN}")
fi

OUT_DIR="$(cd "$(dirname "$AUDIO")" && pwd)"
echo "[whisperx] device=$DEVICE compute=$COMPUTE model=$MODEL" >&2
echo "[whisperx] input=$AUDIO → diarized transcript in $OUT_DIR" >&2
echo "[whisperx] live Sense captions remain sense_whisper_live (ECAPA); this is accurate batch" >&2

run_wx() {
  if command -v whisperx >/dev/null 2>&1; then
    whisperx "$@"
  elif command -v uvx >/dev/null 2>&1; then
    uvx --from whisperx whisperx "$@"
  else
    python -m whisperx "$@"
  fi
}

run_wx "$AUDIO" \
  --model "$MODEL" \
  --language en \
  --diarize \
  --min_speakers 1 \
  --max_speakers 2 \
  --device "$DEVICE" \
  --compute_type "$COMPUTE" \
  --output_dir "$OUT_DIR" \
  "${HF_ARGS[@]}" \
  "${EXTRA[@]}"

echo "[whisperx] done — check ${OUT_DIR}/*.json / *.srt / *.txt for SPEAKER_00/01 labels" >&2
