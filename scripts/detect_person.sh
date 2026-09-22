#!/usr/bin/env bash
# Live laptop webcam → YOLO person boxes. Quit with q.
# Prefer macOS Terminal.app (Camera permission).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
SOURCE="${1:-0}"
exec uv run --with ultralytics yolo predict \
  model=yolov8n.pt \
  source="$SOURCE" \
  classes=0 \
  show=True
