#!/usr/bin/env bash
set -euo pipefail

# Set CSI TCP ingest host/port, rebuild, and flash csi_recv_router.
# USB is only needed for flash; after that the C5 streams CSI_DATA over Wi‑Fi TCP.
#
# Usage (from repo root):
#   ./scripts/set_csi_tcp_host.sh 10.128.93.42
#   ./scripts/set_csi_tcp_host.sh 10.128.93.42 9055
#   ./scripts/set_csi_tcp_host.sh 10.128.93.42 9055 /dev/cu.usbmodem2101
#
# Prerequisite: Wi‑Fi already set (sdkconfig.defaults.local with SSID), or
# combine with set_csi_wifi.sh:
#   CSI_TCP_HOST=10.128.93.42 ./scripts/set_csi_wifi.sh "LabHealthSecurePSK" '…'

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/serial_helpers.sh
source "$ROOT/scripts/serial_helpers.sh"

PROJECT="$ROOT/esp-csi/examples/get-started/csi_recv_router"
LOCAL="$PROJECT/sdkconfig.defaults.local"
SDKCONFIG="$PROJECT/sdkconfig"

HOST="${1:-}"
PORT_NUM="${2:-9055}"
PORT_ARG="${3:-}"

if [[ -z "$HOST" ]]; then
  echo "Usage: $0 <ingest-host-ip> [tcp-port=9055] [serial-port]"
  echo "Example: $0 10.128.93.42 9055"
  echo
  echo "Host should be the LabPSK IP of the Mac Mini running:"
  echo "  cd csi_pipeline_new && ./run_ingest.sh --listen-tcp 9055 --method 4.1 --label …"
  exit 1
fi

if ! [[ "$PORT_NUM" =~ ^[0-9]+$ ]] || (( PORT_NUM < 1 || PORT_NUM > 65535 )); then
  echo "Invalid TCP port: $PORT_NUM"
  exit 1
fi

if ! IDF_ACTIVATE="$(find_idf_activate)"; then
  echo "ESP-IDF activate script not found. Install IDF 6.0.x first."
  exit 1
fi

if ! PORT="$(pick_usb_serial "$PORT_ARG")"; then
  echo "No USB serial port found. Plug in the C5 (needed for flash only)."
  exit 1
fi

# Merge TCP keys into local defaults (preserve Wi‑Fi if present).
python3 - "$LOCAL" "$HOST" "$PORT_NUM" <<'PY'
import re, sys
from pathlib import Path

path, host, port = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8") if path.exists() else (
    "# Local only — do not commit.\n"
)

def upsert(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pat.search(text):
        return pat.sub(line, text, count=1)
    if not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"

text = upsert(text, "CONFIG_CSI_TCP_ENABLE", "y")
text = upsert(text, "CONFIG_CSI_TCP_HOST", f'"{host}"')
text = upsert(text, "CONFIG_CSI_TCP_PORT", port)
path.write_text(text, encoding="utf-8")
print(f"Wrote {path}")
PY

# Patch generated sdkconfig if present
if [[ -f "$SDKCONFIG" ]]; then
  python3 - "$SDKCONFIG" "$HOST" "$PORT_NUM" <<'PY'
import re, sys
path, host, port = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path, encoding="utf-8").read()

def upsert(text, key, value):
    line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pat.search(text):
        return pat.sub(line, text, count=1)
    # Also clear "# CONFIG_CSI_TCP_ENABLE is not set"
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
print(f"Updated {path}")
PY
else
  echo "No sdkconfig yet — first build will pick up sdkconfig.defaults.local"
fi

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -t "$PORT" 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 0.3
  fi
fi

echo "CSI_TCP_HOST=$HOST"
echo "CSI_TCP_PORT=$PORT_NUM"
echo "PORT=$PORT"
echo "Building + flashing…"

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
echo "Done. On the ingest host (same LabPSK):"
echo "  cd csi_pipeline_new"
echo "  ./run_ingest.sh --listen-tcp $PORT_NUM --method 4.1 --label baseline_room_empty"
echo
echo "Reachability check (from a machine that can talk to the C5, or ask IT if isolation blocks):"
echo "  # after C5 joins LabPSK, from Mac Mini:"
echo "  nc -vz <C5_IP>  # not needed; C5 is client → Mini"
echo "  # Mini must accept inbound TCP $PORT_NUM; C5 must reach Mini IP $HOST"
