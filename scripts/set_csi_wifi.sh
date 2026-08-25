#!/usr/bin/env bash
set -euo pipefail

# Change CSI board Wi-Fi, rebuild, and flash.
# Usage (from repo root):
#   ./scripts/set_csi_wifi.sh "SaiPhone" "123456789"
#   ./scripts/set_csi_wifi.sh 'YourSSID' 'YourPassword'
# Optional 3rd arg = serial port, else auto-detect.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/esp-csi/examples/get-started/csi_recv_router"
LOCAL="$PROJECT/sdkconfig.defaults.local"
SDKCONFIG="$PROJECT/sdkconfig"

SSID="${1:-}"
PASS="${2:-}"
PORT_ARG="${3:-}"

if [[ -z "$SSID" || -z "$PASS" ]]; then
  echo "Usage: $0 \"SSID\" \"PASSWORD\" [serial-port]"
  echo "Example: $0 \"SaiPhone\" \"123456789\""
  exit 1
fi

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found. Install IDF 6.0.x first."
  exit 1
fi

if ! PORT="$(pick_usb_serial "$PORT_ARG")"; then
  echo "No USB serial port found. Plug in the C5."
  exit 1
fi

# 1) Write machine-local defaults (gitignored)
cat > "$LOCAL" <<EOF
# Local only — do not commit.
CONFIG_EXAMPLE_WIFI_SSID="$SSID"
CONFIG_EXAMPLE_WIFI_PASSWORD="$PASS"
CONFIG_EXAMPLE_WIFI_AUTH_WPA2_PSK=y
EOF
echo "Wrote $LOCAL"

# 2) Patch generated sdkconfig if present (this is what the build actually uses)
if [[ -f "$SDKCONFIG" ]]; then
  python3 - "$SDKCONFIG" "$SSID" "$PASS" <<'PY'
import re, sys
path, ssid, password = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()
text2, n1 = re.subn(
    r'^CONFIG_EXAMPLE_WIFI_SSID=.*$',
    f'CONFIG_EXAMPLE_WIFI_SSID="{ssid}"',
    text,
    count=1,
    flags=re.M,
)
text2, n2 = re.subn(
    r'^CONFIG_EXAMPLE_WIFI_PASSWORD=.*$',
    f'CONFIG_EXAMPLE_WIFI_PASSWORD="{password}"',
    text2,
    count=1,
    flags=re.M,
)
if n1 != 1 or n2 != 1:
    raise SystemExit(f"sdkconfig Wi-Fi keys not found (ssid={n1}, pass={n2})")
open(path, "w", encoding="utf-8").write(text2)
print(f"Updated {path}")
PY
else
  echo "No sdkconfig yet — first build will pick up sdkconfig.defaults.local"
fi

# Free the serial port if a monitor is holding it (lsof exits 1 when unused)
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -t "$PORT" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 0.3
  fi
fi

echo "SSID=$SSID"
echo "PORT=$PORT"
echo "Building + flashing (do NOT source the repo .venv in this shell)..."

export IDF_ACTIVATE PROJECT PORT
bash --noprofile --norc -c '
  set +u
  # shellcheck source=/dev/null
  source "$IDF_ACTIVATE"
  set -euo pipefail
  command -v riscv32-esp-elf-gcc >/dev/null
  cd "$PROJECT"
  idf.py build flash -p "$PORT" -b 460800
'

echo
echo "Done. Turn on that Wi-Fi / hotspot, then:"
echo "  ./monitor_csi.sh"
echo "Look for: Connecting to $SSID ... connected with $SSID"
