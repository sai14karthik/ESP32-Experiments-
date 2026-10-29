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

cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>WorkingDirectory</key>
  <string>${DIR}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${DIR}/run_publisher.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${DIR}/data/publisher.log</string>
  <key>StandardErrorPath</key>
  <string>${DIR}/data/publisher.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/${UID_NUM}" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_NUM}" "${PLIST}"
launchctl enable "gui/${UID_NUM}/${LABEL}"
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"
echo "publisher service loaded: ${LABEL}"
echo "log: ${DIR}/data/publisher.log"
