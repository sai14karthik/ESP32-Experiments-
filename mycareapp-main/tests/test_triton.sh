#!/usr/bin/env bash
set -euo pipefail

TRITON_HTTP_PORT="${TRITON_HTTP_PORT:-8010}"
BASE_URL="http://localhost:${TRITON_HTTP_PORT}"
MODEL_NAME="${1:-}"

failures=0

check() {
    local label="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "PASS: ${label}"
    else
        echo "FAIL: ${label}" >&2
        failures=$((failures + 1))
    fi
}

format_json() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -m json.tool
    else
        cat
    fi
}

check "Triton readiness" curl -fsS "${BASE_URL}/v2/health/ready"
check "Triton liveness" curl -fsS "${BASE_URL}/v2/health/live"

echo "Models in repository:"
curl -s -X POST "${BASE_URL}/v2/repository/index" -d '{}'
echo ""

if [ -n "${MODEL_NAME}" ]; then
    check "Model '${MODEL_NAME}' readiness" \
        curl -fsS "${BASE_URL}/v2/models/${MODEL_NAME}/ready"
    echo "Metadata for '${MODEL_NAME}':"
    curl -s "${BASE_URL}/v2/models/${MODEL_NAME}" | format_json
fi

if [ "${failures}" -gt 0 ]; then
    echo "${failures} test(s) failed" >&2
    exit 1
fi

echo "All tests passed."
