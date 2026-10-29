#!/usr/bin/env bash

set -euo pipefail

TRITON_HTTP_PORT="${TRITON_HTTP_PORT:-8010}"
TRITON_GRPC_PORT="${TRITON_GRPC_PORT:-8011}"
TRITON_METRICS_PORT="${TRITON_METRICS_PORT:-8012}"
TRITON_EXTERNAL="${TRITON_EXTERNAL:-1}"

cd /rpm

echo "[project] Preparing Blackwell TensorRT..."
source ./scripts/install_trt_blackwell.sh
chmod +x ./scripts/create_model.sh
bash -x ./scripts/create_model.sh

if [ "${TRITON_EXTERNAL}" = "1" ]; then
    echo "[project] Waiting for externally started Triton..."
else
    echo "[project] Starting Triton..."
    tritonserver \
        --model-repository=/rpm/triton_model \
        --http-port="${TRITON_HTTP_PORT}" \
        --grpc-port="${TRITON_GRPC_PORT}" \
        --metrics-port="${TRITON_METRICS_PORT}" &
    triton_pid=$!

    cleanup() {
        kill "${triton_pid}" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT INT TERM
fi

while ! curl -fsS "http://localhost:${TRITON_HTTP_PORT}/v2/health/ready" >/dev/null 2>&1; do
    if [ "${TRITON_EXTERNAL}" = "1" ]; then
        echo "[project] Waiting for externally started Triton..."
        sleep 3
    else
        if ! kill -0 "${triton_pid}" >/dev/null 2>&1; then
            echo "[project] Triton exited before it became ready" >&2
            exit 1
        fi
        echo "[project] Waiting for Triton to be ready..."
        sleep 3
    fi
done

echo "[project] Triton is ready."
echo "[project] Starting DeepStream..."
if [ -x /opt/nvidia/deepstream/deepstream-8.0/bin/deepstream_mycare_app ]; then
    APP_BIN=/opt/nvidia/deepstream/deepstream-8.0/bin/deepstream_mycare_app
else
    APP_BIN=deepstream_mycare_app/deepstream_mycare_app
fi

exec "$APP_BIN" -c project_config.txt -t
