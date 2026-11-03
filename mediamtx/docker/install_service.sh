#!/bin/bash
# Install the camera publisher as a Mac login service.
# MediaMTX stays in Docker. This process uses the Mini's network, restarts
# if it exits, and starts again at login. No terminal.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
# LaunchAgents cannot read ~/Desktop. Run the copy under Application Support.
DIR="${HOME}/Library/Application Support/mediamtx-cameras"
LABEL="com.lakenona.mediamtx-cameras"
WATCH_LABEL="com.lakenona.mediamtx-cameras-watchdog"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
WATCH_PLIST="${HOME}/Library/LaunchAgents/${WATCH_LABEL}.plist"
CREDS="${DIR}/credentials.env"
UID_NUM="$(id -u)"

mkdir -p "${HOME}/Library/LaunchAgents" "${DIR}/data"
chmod 700 "${DIR}" "${DIR}/data"
cp "${SRC}/app.py" "${SRC}/run_publisher.sh" "${SRC}/watchdog.sh" "${DIR}/"
if [[ ! -f "${DIR}/data/cameras.json" && -f "${SRC}/data/cameras.json" ]]; then
  cp "${SRC}/data/cameras.json" "${DIR}/data/cameras.json"
fi
if [[ -f "${DIR}/data/cameras.json" ]]; then
  chmod 600 "${DIR}/data/cameras.json"
fi
chmod +x "${DIR}/run_publisher.sh" "${DIR}/watchdog.sh"

if [[ ! -f "${CREDS}" ]]; then
  python3 - "${CREDS}" <<'PY'
import os, secrets, sys

path = sys.argv[1]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write("MTX_PUBLISH_USER=publisher\n")
    fh.write(f"MTX_PUBLISH_PASSWORD={secrets.token_hex(32)}\n")
    fh.write("MTX_API_USER=admin\n")
    fh.write(f"MTX_API_PASSWORD={secrets.token_hex(32)}\n")
PY
fi
chmod 600 "${CREDS}"

(cd "${SRC}" && docker compose --env-file "${CREDS}" stop cameras >/dev/null 2>&1 || true)
(cd "${SRC}" && docker compose --env-file "${CREDS}" up -d mediamtx)

# A bare LaunchAgent cannot see the LAN. An app bundle can, after one Allow.
# codesign --force clears that Allow. Only resign when the bundle contents change.
BUNDLE="${HOME}/Applications/MediaMTX Cameras.app"
mkdir -p "${BUNDLE}/Contents/MacOS"
INFO_PLIST="${BUNDLE}/Contents/Info.plist"
PUBLISHER_BIN="${BUNDLE}/Contents/MacOS/publisher"
INFO_NEW="$(mktemp)"
PUB_NEW="$(mktemp)"
trap 'rm -f "${INFO_NEW}" "${PUB_NEW}"' EXIT
cat > "${INFO_NEW}" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>publisher</string>
<key>CFBundleIdentifier</key><string>com.lakenona.mediamtx-cameras</string>
<key>CFBundleName</key><string>MediaMTX Cameras</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.1</string>
<key>LSBackgroundOnly</key><true/>
<key>NSLocalNetworkUsageDescription</key>
<string>Publishes lab cameras to MediaMTX.</string>
<key>NSBonjourServices</key>
<array>
  <string>_rtsp._tcp</string>
  <string>_http._tcp</string>
</array>
</dict></plist>
EOF
cat > "${PUB_NEW}" <<EOF
#!/bin/bash
cd "${DIR}" || exit 1
exec /bin/bash ./run_publisher.sh
EOF
chmod 755 "${PUB_NEW}"
NEED_SIGN=0
if ! cmp -s "${INFO_NEW}" "${INFO_PLIST}" 2>/dev/null; then
  cp "${INFO_NEW}" "${INFO_PLIST}"
  NEED_SIGN=1
fi
if ! cmp -s "${PUB_NEW}" "${PUBLISHER_BIN}" 2>/dev/null; then
  cp "${PUB_NEW}" "${PUBLISHER_BIN}"
  chmod 755 "${PUBLISHER_BIN}"
  NEED_SIGN=1
fi
if [[ "${NEED_SIGN}" -eq 1 ]] || ! codesign --verify --quiet "${BUNDLE}" 2>/dev/null; then
  codesign --force --sign - "${BUNDLE}" >/dev/null
  echo "signed MediaMTX Cameras.app — toggle Local Network off/on for this app once"
fi

python3 - "${PLIST}" "${LABEL}" "${DIR}" "${BUNDLE}" <<'PY'
import os, plistlib, sys
plist_path, label, directory, bundle = sys.argv[1:]
os.makedirs(os.path.dirname(plist_path), exist_ok=True)
os.makedirs(os.path.join(directory, "data"), exist_ok=True)
with open(plist_path, "wb") as fh:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": ["/usr/bin/open", "-W", "-g", "-a", bundle],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.join(directory, "data", "publisher.log"),
        "StandardErrorPath": os.path.join(directory, "data", "publisher.log"),
    }, fh)
    os.chmod(plist_path, 0o600)
PY

python3 - "${WATCH_PLIST}" "${WATCH_LABEL}" "${DIR}" <<'PY'
import os, plistlib, sys
plist_path, label, directory = sys.argv[1:]
watch = os.path.join(directory, "watchdog.sh")
with open(plist_path, "wb") as fh:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": ["/bin/bash", watch],
        "StartInterval": 120,
        "RunAtLoad": True,
        "StandardOutPath": os.path.join(directory, "data", "watchdog.log"),
        "StandardErrorPath": os.path.join(directory, "data", "watchdog.log"),
    }, fh)
    os.chmod(plist_path, 0o600)
PY

launchctl bootout "gui/${UID_NUM}" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootout "gui/${UID_NUM}" "${WATCH_PLIST}" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST}"; then
  launchctl load -w "${PLIST}"
fi
if ! launchctl bootstrap "gui/${UID_NUM}" "${WATCH_PLIST}"; then
  launchctl load -w "${WATCH_PLIST}"
fi
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}" || true
echo "publisher service loaded: ${LABEL}"
echo "watchdog loaded: ${WATCH_LABEL} (every 120s)"
echo "log: ${DIR}/data/publisher.log"
echo "If cameras stay 404: System Settings → Privacy & Security → Local Network → MediaMTX Cameras → off/on"
