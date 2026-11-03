#!/bin/bash
# Mini-only: check MediaMTX paths + cameras; recover without killing a good publisher.
# Usage:
#   ./health_cameras.sh           # status only
#   ./health_cameras.sh --fix     # recover (POST /restart, or start if 8090 free)
set -eu

DIR="${HOME}/Library/Application Support/mediamtx-cameras"
CREDS="${DIR}/credentials.env"
LOG="${DIR}/data/publisher.log"
HEALTH_LOG="${DIR}/data/health.log"
FIX=0
QUIET=0
case "${1:-}" in
  --fix|-f) FIX=1 ;;
  --fix-quiet|-q) FIX=1; QUIET=1 ;;
esac

logh() {
  mkdir -p "${DIR}/data"
  echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >>"${HEALTH_LOG}"
  if [ "${QUIET}" -eq 0 ]; then
    echo "$*"
  fi
}

if [ ! -f "${CREDS}" ]; then
  echo "missing ${CREDS} - run this on the Mini" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${CREDS}"
set +a

path_code() {
  local out
  out=$(curl -s -u "${MTX_API_USER}:${MTX_API_PASSWORD}" -o /dev/null -w "%{http_code}" \
    "http://127.0.0.1:9997/v3/paths/get/${1}" 2>/dev/null) || out="000"
  printf '%s' "${out}"
}

if [ "${QUIET}" -eq 0 ]; then
  echo "=== paths ==="
fi
DOWN=0
for p in cam_sense cam_sense2 cam_sense3 cam_sense4; do
  code=$(path_code "${p}")
  if [ "${QUIET}" -eq 0 ]; then
    echo "${p} ${code}"
  fi
  if [ "${code}" != "200" ]; then
    DOWN=1
  fi
done

PID=""
PID=$(lsof -nP -iTCP:8090 -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)
if [ "${QUIET}" -eq 0 ]; then
  echo "=== publisher :8090 ==="
  if [ -n "${PID}" ]; then
    echo "listening pid=${PID}"
  else
    echo "8090 free"
  fi
  echo "=== cameras ==="
  python3 - <<'PY'
import socket
cams = [
    ("cam_sense", "10.128.93.15", 81),
    ("cam_sense2", "10.128.93.24", 81),
    ("cam_sense3", "10.128.93.60", 554),
    ("cam_sense4", "10.128.93.61", 554),
]
for name, host, port in cams:
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, port))
        print("%s %s:%s ok" % (name, host, port))
    except Exception as e:
        print("%s %s:%s FAIL %s" % (name, host, port, e))
    finally:
        s.close()
PY
fi

if [ "${FIX}" -ne 1 ]; then
  echo
  echo "status only. To recover: $0 --fix"
  exit 0
fi

if [ "${DOWN}" -eq 0 ] && [ -n "${PID}" ]; then
  logh "ok all paths 200"
  exit 0
fi

echo "=== fix ==="
if [ -z "${PID}" ]; then
  logh "8090 free - starting publisher"
  cd "${DIR}"
  nohup ./run_publisher.sh >>"${LOG}" 2>&1 &
  disown || true
  sleep 10
elif [ "${DOWN}" -eq 1 ]; then
  # Prefer in-process restart so the SSH/nohup publisher keeps its network rights.
  logh "paths down - POST /restart pid=${PID}"
  code=$(curl -s -u "${MTX_API_USER}:${MTX_API_PASSWORD}" -o /tmp/mediamtx-restart.json -w "%{http_code}" \
    -X POST "http://127.0.0.1:8090/restart" || echo "000")
  if [ "${code}" != "200" ]; then
    logh "POST /restart failed http=${code} - hard restart"
    kill -9 "${PID}" 2>/dev/null || true
    pkill -9 -f 'run_publisher.sh' 2>/dev/null || true
    pkill -9 -f 'Python -u app.py' 2>/dev/null || true
    sleep 2
    cd "${DIR}"
    nohup ./run_publisher.sh >>"${LOG}" 2>&1 &
    disown || true
  fi
  sleep 10
else
  logh "ok"
  exit 0
fi

if [ "${QUIET}" -eq 0 ]; then
  echo "=== paths after fix ==="
fi
for p in cam_sense cam_sense2 cam_sense3 cam_sense4; do
  code=$(path_code "${p}")
  if [ "${QUIET}" -eq 0 ]; then
    echo "${p} ${code}"
  fi
  logh "after ${p} ${code}"
done
