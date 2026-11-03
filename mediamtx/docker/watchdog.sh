#!/bin/bash
# If the publisher is down, start the signed app (Local Network aware).
# If it is up, leave it alone. Safe to run every few minutes from launchd.
set -euo pipefail
DIR="${HOME}/Library/Application Support/mediamtx-cameras"
APP="${HOME}/Applications/MediaMTX Cameras.app"
LOG="${DIR}/data/watchdog.log"
mkdir -p "${DIR}/data"
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') $*" >>"${LOG}"; }

if lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1; then
  exit 0
fi

log "8090 down — starting MediaMTX Cameras"
open -g -a "${APP}"
sleep 8
if lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1; then
  log "publisher up"
  exit 0
fi

# App start failed (Local Network). Fall back to host script from this agent.
log "app start failed — falling back to run_publisher.sh"
cd "${DIR}"
nohup ./run_publisher.sh >>"${DIR}/data/publisher.log" 2>&1 &
disown
sleep 5
if lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1; then
  log "fallback publisher up"
else
  log "fallback publisher failed"
  exit 1
fi
