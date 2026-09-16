#!/usr/bin/env bash
# SoftAP video + optional USB internet.
#
#   Mac Wi‑Fi  → XIAO-CAM (must show as current network) → video
#   Mac USB    → iPhone Personal Hotspot (optional) → internet
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

wifi_ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
ssid="$(networksetup -getairportnetwork en0 2>/dev/null || true)"
echo "Wi‑Fi (en0): ${wifi_ip:-none}  ($ssid)" >&2

# Stale 192.168.4.2 after SoftAP drops does NOT count — need real association.
if [[ "$ssid" != *"XIAO-CAM"* ]]; then
  cat >&2 <<'EOF'
Mac is NOT joined to Wi‑Fi "XIAO-CAM" right now.

(A leftover 192.168.4.x IP does not count.)

1) Wi‑Fi menu → join XIAO-CAM / password 12345678
2) Confirm: networksetup -getairportnetwork en0
   → must say: Current Wi-Fi Network: XIAO-CAM
3) Re-run: ./scripts/run_softap_with_internet.sh

If the board was just reset/flashed, wait ~5s then join again.
EOF
  exit 1
fi

if [[ "$wifi_ip" != 192.168.4.* ]]; then
  echo "On XIAO-CAM but IP is ${wifi_ip:-none} — wait for DHCP, or toggle Wi‑Fi." >&2
  exit 1
fi

if ! ping -c 1 -W 1000 8.8.8.8 >/dev/null 2>&1; then
  echo "NOTE: no internet. Plug iPhone USB + Personal Hotspot if you need Cursor." >&2
else
  echo "Internet OK." >&2
fi

exec "$ROOT/scripts/run_softap_cam.sh"
