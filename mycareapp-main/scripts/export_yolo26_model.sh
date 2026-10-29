#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

MODEL_DATA_DIR="${MODEL_DATA_DIR:-/models}"
MODEL_PT_PATH="${YOLO26_MODEL_PT_PATH:-${MODEL_DATA_DIR}/yolo26_m_pose.pt}"
MODEL_ONNX_PATH="${YOLO26_MODEL_ONNX_PATH:-${MODEL_DATA_DIR}/yolo26_m_pose.onnx}"

mkdir -p "${MODEL_DATA_DIR}"

if [ ! -f "${MODEL_PT_PATH}" ]; then
    echo "[yolo26] Downloading model.pt..."
    curl -fsSL https://huggingface.co/openvision/yolo26-m-pose/resolve/main/model.pt \
        -o "${MODEL_PT_PATH}"
fi

if ! python3 -c "import ultralytics" >/dev/null 2>&1; then
    echo "[yolo26] Installing Ultralytics..."
    python3 -m pip install --quiet ultralytics
fi

echo "[yolo26] Exporting ONNX..."
python3 - "${MODEL_PT_PATH}" "${MODEL_ONNX_PATH}" <<'PY'
import os
import sys

from ultralytics import YOLO

pt_path, onnx_path = sys.argv[1:]
model = YOLO(pt_path)
exported = model.export(
    format="onnx",
    imgsz=640,
    dynamic=False,
    simplify=True,
    opset=17,
    nms=False,
)
print(exported)
if os.path.abspath(exported) != os.path.abspath(onnx_path):
    os.replace(exported, onnx_path)
PY
