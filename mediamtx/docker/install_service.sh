#!/bin/bash
# Install the camera publisher as a Mac login service.
# MediaMTX stays in Docker. This process uses the Mini's network, restarts
# if it exits, and starts again at login. No terminal.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
# LaunchAgents cannot read ~/Desktop. Run the copy under Application Support.
DIR="${HOME}/Library/Application Support/mediamtx-cameras"
LABEL="com.lakenona.mediamtx-cameras"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

mkdir -p "${HOME}/Library/LaunchAgents" "${DIR}/data" "${SRC}/data"
cp "${SRC}/app.py" "${SRC}/run_publisher.sh" "${DIR}/"
if [[ -f "${SRC}/data/cameras.json" ]]; then
  cp "${SRC}/data/cameras.json" "${DIR}/data/cameras.json"
fi
chmod +x "${DIR}/run_publisher.sh"

(cd "${SRC}" && docker compose stop cameras >/dev/null 2>&1 || true)
(cd "${SRC}" && docker compose up -d mediamtx)

# A bare LaunchAgent cannot see the LAN. An app bundle can, after one Allow.
BUNDLE="${HOME}/Applications/MediaMTX Cameras.app"
mkdir -p "${BUNDLE}/Contents/MacOS"
cat > "${BUNDLE}/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>publisher</string>
<key>CFBundleIdentifier</key><string>com.lakenona.mediamtx-cameras</string>
<key>CFBundleName</key><string>MediaMTX Cameras</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSBackgroundOnly</key><true/>
<key>NSLocalNetworkUsageDescription</key>
<string>Publishes lab cameras to MediaMTX.</string>
</dict></plist>
EOF
cat > "${BUNDLE}/Contents/MacOS/publisher" <<EOF
#!/bin/bash
cd "${DIR}" || exit 1
exec /bin/bash ./run_publisher.sh
EOF
chmod 755 "${BUNDLE}/Contents/MacOS/publisher"
codesign --force --sign - "${BUNDLE}" >/dev/null

python3 - "${PLIST}" "${LABEL}" "${DIR}" "${BUNDLE}" <<'PY'
import os, plistlib, sys
plist_path, label, directory, bundle = sys.argv[1:]
os.makedirs(os.path.dirname(plist_path), exist_ok=True)
os.makedirs(os.path.join(directory, "data"), exist_ok=True)
exe = os.path.join(bundle, "Contents", "MacOS", "publisher")
with open(plist_path, "wb") as fh:
    plistlib.dump({
        "Label": label,
        "WorkingDirectory": directory,
        "ProgramArguments": [exe],
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
