#!/usr/bin/env bash

set -euo pipefail

TRITON_HTTP_PORT="${TRITON_HTTP_PORT:-8010}"
TRITON_GRPC_PORT="${TRITON_GRPC_PORT:-8011}"
TRITON_METRICS_PORT="${TRITON_METRICS_PORT:-8012}"

cd /rpm

echo "[triton] Preparing Blackwell TensorRT..."
source ./scripts/install_trt_blackwell.sh
chmod +x ./scripts/create_model.sh
bash -x ./scripts/create_model.sh

echo "[triton] Starting Triton..."
exec tritonserver \
    --model-repository=/rpm/triton_model \
    --http-port="${TRITON_HTTP_PORT}" \
    --grpc-port="${TRITON_GRPC_PORT}" \
    --metrics-port="${TRITON_METRICS_PORT}"
