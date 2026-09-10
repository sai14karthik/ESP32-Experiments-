#!/usr/bin/env bash
set -euo pipefail

# Change CSI board Wi-Fi, rebuild, and flash.
# Usage (from repo root):
#   ./scripts/set_csi_wifi.sh "SaiPhone" "123456789"
#   ./scripts/set_csi_wifi.sh 'YourSSID' 'YourPassword'
# Optional 3rd arg = serial port, else auto-detect.
#
# Optional TCP ingest host (wireless CSI, no USB for data after flash):
#   CSI_TCP_HOST=10.128.93.42 ./scripts/set_csi_wifi.sh "SSID" "PASS"
#   CSI_TCP_HOST=10.128.93.42 CSI_TCP_PORT=9055 ./scripts/set_csi_wifi.sh "SSID" "PASS" [port]
# Or set TCP alone later: ./scripts/set_csi_tcp_host.sh 10.128.93.42

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
TCP_HOST="${CSI_TCP_HOST:-}"
TCP_PORT="${CSI_TCP_PORT:-9055}"
# Preserve prior TCP settings when only updating Wi‑Fi
if [[ -z "$TCP_HOST" && -f "$LOCAL" ]]; then
  TCP_HOST="$(grep -E '^CONFIG_CSI_TCP_HOST=' "$LOCAL" 2>/dev/null | sed 's/^CONFIG_CSI_TCP_HOST="//;s/"$//' || true)"
  PREV_PORT="$(grep -E '^CONFIG_CSI_TCP_PORT=' "$LOCAL" 2>/dev/null | cut -d= -f2 || true)"
  if [[ -n "${PREV_PORT:-}" ]]; then
    TCP_PORT="$PREV_PORT"
  fi
fi
{
  echo "# Local only — do not commit."
  echo "CONFIG_EXAMPLE_WIFI_SSID=\"$SSID\""
  echo "CONFIG_EXAMPLE_WIFI_PASSWORD=\"$PASS\""
  echo "CONFIG_EXAMPLE_WIFI_AUTH_WPA2_PSK=y"
  if [[ -n "$TCP_HOST" ]]; then
    echo "CONFIG_CSI_TCP_ENABLE=y"
    echo "CONFIG_CSI_TCP_HOST=\"$TCP_HOST\""
    echo "CONFIG_CSI_TCP_PORT=$TCP_PORT"
  fi
} > "$LOCAL"
echo "Wrote $LOCAL"
if [[ -n "$TCP_HOST" ]]; then
  echo "CSI TCP forward → $TCP_HOST:$TCP_PORT"
fi

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
  if [[ -n "$TCP_HOST" ]]; then
    python3 - "$SDKCONFIG" "$TCP_HOST" "$TCP_PORT" <<'PY'
import re, sys
path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()

def upsert(text, key, value):
    line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pat.search(text):
        return pat.sub(line, text, count=1)
    not_set = re.compile(rf"^# {re.escape(key)} is not set\s*$", re.M)
    if not_set.search(text):
        return not_set.sub(line, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"

text = upsert(text, "CONFIG_CSI_TCP_ENABLE", "y")
text = upsert(text, "CONFIG_CSI_TCP_HOST", f'"{host}"')
text = upsert(text, "CONFIG_CSI_TCP_PORT", port)
open(path, "w", encoding="utf-8").write(text)
print(f"Updated TCP keys in {path}")
PY
  fi
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
