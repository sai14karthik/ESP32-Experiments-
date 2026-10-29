#!/bin/bash
# Install the camera publisher as a Mac login service.
# MediaMTX stays in Docker. This process uses the Mini's network, restarts
# if it exits, and starts again at login. No terminal.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.lakenona.mediamtx-cameras"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

mkdir -p "${HOME}/Library/LaunchAgents" "${DIR}/data"
chmod +x "${DIR}/run_publisher.sh"

(cd "${DIR}" && docker compose stop cameras >/dev/null 2>&1 || true)
(cd "${DIR}" && docker compose up -d mediamtx)

python3 - "${PLIST}" "${LABEL}" "${DIR}" <<'PY'
import os, plistlib, sys
plist_path, label, directory = sys.argv[1:]
os.makedirs(os.path.dirname(plist_path), exist_ok=True)
os.makedirs(os.path.join(directory, "data"), exist_ok=True)
with open(plist_path, "wb") as fh:
    plistlib.dump({
        "Label": label,
        "WorkingDirectory": directory,
        "ProgramArguments": ["/bin/bash", os.path.join(directory, "run_publisher.sh")],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.join(directory, "data", "publisher.log"),
        "StandardErrorPath": os.path.join(directory, "data", "publisher.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }, fh)
os.chmod(plist_path, 0o644)
PY

launchctl bootout "gui/${UID_NUM}" "${PLIST}" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST}"; then
  launchctl load -w "${PLIST}"
fi
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" || true
echo "publisher service loaded: ${LABEL}"
echo "log: ${DIR}/data/publisher.log"
