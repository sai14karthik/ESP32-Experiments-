#!/bin/bash
# One-time: allow ./scripts/run_record.sh without a password prompt.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/sudoers.wifi_pose"
DST="/etc/sudoers.d/wifi_pose"
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
if sudo -n -l 2>/dev/null | grep -q 'wifi_pose'; then
  echo "Done. No password on: $ROOT/scripts/run_record.sh"
else
  echo "Installed at $DST. Try: $ROOT/scripts/run_record.sh --help"
fi
