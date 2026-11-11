#!/bin/bash
# One-time: allow run_live.sh without a password prompt.
# macOS still needs root for librealsense; this only skips the password.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/sudoers.realsense"
DST="/etc/sudoers.d/realsense_skeleton"
if [[ ! -f "$SRC" ]]; then
  echo "missing $SRC" >&2
  exit 1
fi
tmp="$(mktemp)"
cp "$SRC" "$tmp"
chmod 440 "$tmp"
if ! visudo -cf "$tmp"; then
  echo "sudoers file failed validation" >&2
  rm -f "$tmp"
  exit 1
fi
echo "Installing $DST (admin password once)..."
osascript -e "do shell script \"cp '$tmp' '$DST' && chmod 440 '$DST' && chown root:wheel '$DST'\" with administrator privileges"
rm -f "$tmp"
if [[ -f "$DST" ]] && sudo -n -l 2>/dev/null | grep -q 'realsense_skeleton/live_joints.py'; then
  echo "Done. No password on: $ROOT/run_live.sh"
else
  echo "Installed at $DST. Try: $ROOT/run_live.sh"
fi
