#!/bin/bash
set -e

sanitize_ld_preload() {
    local preload="" entry
    if [ -n "${LD_PRELOAD:-}" ]; then
        while IFS= read -r -d ':' entry || [ -n "$entry" ]; do
            if [ -n "$entry" ] && [ -e "$entry" ]; then
                preload="${preload:+$preload:}$entry"
            fi
        done <<< "$LD_PRELOAD"

        if [ -n "$preload" ]; then
            export LD_PRELOAD="$preload"
        else
            unset LD_PRELOAD
        fi
    fi
}

sanitize_ld_preload

TRITON_MODEL_PATH="triton_model"
MODEL_ONNX_PATH="/models"

RPM_PCT_FILE="rpm_pct_model_1.onnx"
GDINO_FILE="gdino_model_latest_halfres.onnx"
AR_FILE="rpm_skateformer_model_sdg.onnx"

is_blackwell=0
if command -v nvidia-smi >/dev/null 2>&1; then
    if [ "$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | cut -d. -f1-2)" = "12.0" ]; then
        is_blackwell=1
    fi
fi

TRTEXEC_BIN="$(command -v trtexec 2>/dev/null || true)"
if [ -z "$TRTEXEC_BIN" ] && [ -x /usr/src/tensorrt/bin/trtexec ]; then
    TRTEXEC_BIN=/usr/src/tensorrt/bin/trtexec
fi

if [ "$is_blackwell" -eq 1 ]; then
    trtexec_version="$(trtexec --help 2>/dev/null | sed -n 's/.*TensorRT v\([0-9]*\).*/\1/p' | head -1)"
    if [ "$trtexec_version" != "100800" ] && [ "$trtexec_version" != "10.8" ]; then
        echo "[create_model] Blackwell requires TensorRT 10.8. Use rpm-ds:1.0-blackwell-dev." >&2
        exit 1
    fi
fi

if [ "$is_blackwell" -eq 1 ] && [ ! -f "${TRITON_MODEL_PATH}/.trt10_8-built" ]; then
    echo "[create_model] Removing incompatible TensorRT plans for Blackwell..."
    rm -f "${TRITON_MODEL_PATH}/hpe/1/model.plan" \
        "${TRITON_MODEL_PATH}/gdino_trt/1/model.plan" \
        "${TRITON_MODEL_PATH}/ar/1/model.plan" \
        "${TRITON_MODEL_PATH}/yolo26_m_pose/1/model.plan"
fi

if [ ! -f "${TRITON_MODEL_PATH}/hpe/1/model.plan" ]; then
    "$TRTEXEC_BIN" --onnx=${MODEL_ONNX_PATH}/${RPM_PCT_FILE} --fp16 --useCudaGraph --saveEngine=${TRITON_MODEL_PATH}/hpe/1/model.plan
fi

if [ ! -f "${TRITON_MODEL_PATH}/gdino_trt/1/model.plan" ]; then
    "$TRTEXEC_BIN" --onnx=${MODEL_ONNX_PATH}/${GDINO_FILE} \
        --minShapes=inputs:1x3x540x960,input_ids:1x256,attention_mask:1x256,position_ids:1x256,token_type_ids:1x256,text_token_mask:1x256x256 \
        --optShapes=inputs:4x3x540x960,input_ids:4x256,attention_mask:4x256,position_ids:4x256,token_type_ids:4x256,text_token_mask:4x256x256 \
        --maxShapes=inputs:4x3x540x960,input_ids:4x256,attention_mask:4x256,position_ids:4x256,token_type_ids:4x256,text_token_mask:4x256x256 \
        --fp16 --useCudaGraph \
        --saveEngine=${TRITON_MODEL_PATH}/gdino_trt/1/model.plan
fi

if [ ! -f "${TRITON_MODEL_PATH}/ar/1/model.plan" ]; then
    "$TRTEXEC_BIN" --onnx=${MODEL_ONNX_PATH}/${AR_FILE} \
        --minShapes=input:1x3x64x18x1 \
        --optShapes=input:1x3x64x18x1 \
        --maxShapes=input:1x3x64x18x1 \
        --fp16 --useCudaGraph \
        --saveEngine=${TRITON_MODEL_PATH}/ar/1/model.plan
fi

if [ -f "${MODEL_ONNX_PATH}/yolo26_m_pose.onnx" ] && [ ! -f "${TRITON_MODEL_PATH}/yolo26_m_pose/1/model.plan" ]; then
    "$TRTEXEC_BIN" --onnx=${MODEL_ONNX_PATH}/yolo26_m_pose.onnx \
        --fp16 --useCudaGraph \
        --saveEngine=${TRITON_MODEL_PATH}/yolo26_m_pose/1/model.plan
fi

if [ "$is_blackwell" -eq 1 ]; then
    touch "${TRITON_MODEL_PATH}/.trt10_8-built"
fi
