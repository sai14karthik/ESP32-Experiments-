#!/bin/bash
# Install a Mini LaunchAgent that auto-recovers camera paths every 2 minutes.
# Does NOT use MediaMTX Cameras.app (Local Network breaks that path).
# Keeps / starts the host publisher via nohup, and prefers POST /restart.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="${HOME}/Library/Application Support/mediamtx-cameras"
LABEL="com.lakenona.mediamtx-cameras-health"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

mkdir -p "${HOME}/Library/LaunchAgents" "${DIR}/data"
chmod 700 "${DIR}" "${DIR}/data"
cp "${SRC}/app.py" "${SRC}/run_publisher.sh" "${SRC}/health_cameras.sh" "${DIR}/"
chmod +x "${DIR}/run_publisher.sh" "${DIR}/health_cameras.sh"

# Leave the broken app agents unloaded.
launchctl bootout "gui/${UID_NUM}/com.lakenona.mediamtx-cameras" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_NUM}/com.lakenona.mediamtx-cameras-watchdog" >/dev/null 2>&1 || true

python3 - "${PLIST}" "${LABEL}" "${DIR}" <<'PY'
import os, plistlib, sys
plist_path, label, directory = sys.argv[1:]
script = os.path.join(directory, "health_cameras.sh")
with open(plist_path, "wb") as fh:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": ["/bin/bash", script, "--fix-quiet"],
        "StartInterval": 120,
        "RunAtLoad": True,
        "StandardOutPath": os.path.join(directory, "data", "health.log"),
        "StandardErrorPath": os.path.join(directory, "data", "health.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }, fh)
os.chmod(plist_path, 0o600)
PY

launchctl bootout "gui/${UID_NUM}" "${PLIST}" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST}"; then
  launchctl load -w "${PLIST}"
fi
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" || true

# One hard restart from this SSH/install shell so new app.py (POST /restart) is loaded.
pkill -9 -f 'run_publisher.sh' 2>/dev/null || true
pkill -9 -f 'Python -u app.py' 2>/dev/null || true
sleep 2
cd "${DIR}"
nohup ./run_publisher.sh >>"${DIR}/data/publisher.log" 2>&1 &
disown || true
sleep 8
echo "publisher restarted with new app.py"

echo "health agent loaded: ${LABEL} (every 120s)"
echo "log: ${DIR}/data/health.log"
echo "manual status: ${DIR}/health_cameras.sh"
