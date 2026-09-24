#!/usr/bin/env bash
# Offline file diarization via local clone of MahmoudAshraf97/whisper-diarization.
#
# NOT for live Sense captions. Live path stays:
#   ./scripts/sense_whisper_live.sh --ip … --diarize-backend ecapa
#
# This is post-hoc quality (full file → word-level speakers). Needs CUDA + NeMo;
# Apple Metal Mini is a poor fit — run on a CUDA box / GPU cluster.
#
# Setup once (CUDA machine):
#   git clone https://github.com/MahmoudAshraf97/whisper-diarization
#   cd whisper-diarization && pip install -c constraints.txt -r requirements.txt
#
# Usage:
#   ./scripts/sense_diarize_offline.sh /path/to/recording.wav
#   ./scripts/sense_diarize_offline.sh recording.wav --diarizer sortformer
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WD="${WHISPER_DIARIZATION_DIR:-$ROOT/whisper-diarization}"

if [[ ! -f "$WD/diarize.py" ]]; then
  echo "Missing $WD/diarize.py" >&2
  echo "Clone: git clone https://github.com/MahmoudAshraf97/whisper-diarization \"$WD\"" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 AUDIO_FILE [--diarizer msdd|sortformer] [extra diarize.py args…]" >&2
  exit 1
fi

AUDIO="$1"
shift

echo "[offline] whisper-diarization → $AUDIO" >&2
echo "[offline] live Sense captions still use sense_whisper_live (ECAPA/mlx), not this" >&2
cd "$WD"
exec python diarize.py -a "$AUDIO" "$@"
