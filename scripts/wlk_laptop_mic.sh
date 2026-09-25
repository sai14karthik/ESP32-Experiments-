#!/usr/bin/env bash
# Compare WhisperLiveKit + Diart on the *laptop mic* (not Sense UDP).
#
# One-time setup (from repo root):
#   ./scripts/wlk_laptop_mic.sh --install
#
# Run (browser uses laptop mic):
#   export HF_TOKEN=…   # accept pyannote/segmentation, segmentation-3.0, embedding
#   ./scripts/wlk_laptop_mic.sh
#   open http://127.0.0.1:8000
#
# Sense live captions stay: ./scripts/sense_whisper_live.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WLK="$ROOT/WhisperLiveKitDiarization"
VENV="$WLK/.venv"

if [[ ! -d "$WLK" ]]; then
  echo "Missing $WLK — clone WhisperLiveKitDiarization first" >&2
  exit 1
fi

if [[ "${1:-}" == "--install" ]]; then
  shift
  echo "[wlk] creating venv + installing diarization + mlx-whisper …" >&2
  python3 -m venv "$VENV"
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  pip install -U pip wheel
  pip install -e "$WLK[diarization,mlx-whisper,vac]"
  # diart/pyannote.audio 3.x needs torchaudio with AudioMetaData (broken in 2.9+)
  pip install 'torch==2.5.1' 'torchaudio==2.5.1' 'torchvision==0.20.1'
  # diart still passes use_auth_token= (removed in huggingface_hub 0.26+)
  pip install 'huggingface_hub>=0.23,<0.26'
  echo "[wlk] install done. Accept ALL of these HF gated models (same account as HF_TOKEN):" >&2
  echo "  https://huggingface.co/pyannote/segmentation" >&2
  echo "  https://huggingface.co/pyannote/segmentation-3.0" >&2
  echo "  https://huggingface.co/pyannote/embedding" >&2
  echo "Then: huggingface-cli login   OR   export HF_TOKEN=…" >&2
  exit 0
fi

if [[ ! -x "$VENV/bin/whisperlivekit-server" ]]; then
  echo "[wlk] not installed — run: $0 --install" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Prefer env; else Hugging Face CLI cache
if [[ -z "${HF_TOKEN:-}" && -z "${HUGGINGFACE_HUB_TOKEN:-}" && -f "${HOME}/.cache/huggingface/token" ]]; then
  export HF_TOKEN
  HF_TOKEN="$(cat "${HOME}/.cache/huggingface/token")"
  export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"
fi

HOST="${WLK_HOST:-127.0.0.1}"
PORT="${WLK_PORT:-8000}"
MODEL="${WLK_MODEL:-large-v3-turbo}"

echo "[wlk] laptop-mic compare @ http://${HOST}:${PORT}" >&2
echo "[wlk] model=$MODEL backend=mlx-whisper diarization=Diart (not Sense UDP)" >&2
if [[ -z "${HF_TOKEN:-${HUGGINGFACE_HUB_TOKEN:-}}" ]]; then
  echo "[wlk] WARNING: HF_TOKEN not set — Diart/pyannote may 403 until you login" >&2
fi

DIARIZE=1
EXTRA=()
for a in "$@"; do
  case "$a" in
    --no-diarization) DIARIZE=0 ;;
    *) EXTRA+=("$a") ;;
  esac
done

ARGS=(
  --host "$HOST"
  --port "$PORT"
  --model "$MODEL"
  --backend mlx-whisper
  --lan en
  --vac
)
if [[ "$DIARIZE" == "1" ]]; then
  ARGS+=(--diarization)
  echo "[wlk] diarization ON (needs HF accept: pyannote/segmentation-3.0 + pyannote/embedding)" >&2
else
  echo "[wlk] diarization OFF (ASR only) — re-run without --no-diarization after accepting embedding" >&2
fi

if [[ ${#EXTRA[@]} -gt 0 ]]; then
  exec whisperlivekit-server "${ARGS[@]}" "${EXTRA[@]}"
else
  exec whisperlivekit-server "${ARGS[@]}"
fi
