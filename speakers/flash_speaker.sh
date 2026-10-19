#!/bin/sh
# Build and flash one XiaoZhi speaker from a speakers/<name>.env file.
# Wi-Fi saved on the board is left alone (NVS is not erased).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ENV_FILE=${1:-}
PORT_ARG=${2:-}

if [ -n "$ENV_FILE" ] && [ ! -f "$ENV_FILE" ] && [ -f "$ROOT/$ENV_FILE" ]; then
  ENV_FILE="$ROOT/$ENV_FILE"
fi
if [ -z "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "Usage: $0 speakers/<name>.env [usb-port]" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$ENV_FILE"

: "${SPEAKER_ID:?SPEAKER_ID is required}"
: "${WAKE_PHRASE:?WAKE_PHRASE is required}"
: "${WAKE_DISPLAY:?WAKE_DISPLAY is required}"
: "${WAKE_THRESHOLD:?WAKE_THRESHOLD is required}"
ENV_ID=$(basename "$ENV_FILE" .env)
if [ "$ENV_ID" != "$SPEAKER_ID" ]; then
  echo "SPEAKER_ID ($SPEAKER_ID) must match the file name ($ENV_ID.env)" >&2
  exit 1
fi
BOARD=${BOARD:-waveshare/esp32-s3-audio-board}
LANGUAGE=${LANGUAGE:-en-US}
XIAOZHI_TAG=${XIAOZHI_TAG:-v2.5.2}
PORT=${PORT_ARG:-${PORT:-/dev/cu.usbmodem101}}

IDF_ACTIVATE=${IDF_ACTIVATE:-"$HOME/.espressif/tools/activate_idf_v6.0.2.sh"}
SRC="$ROOT/.src/xiaozhi-esp32"
PATCH="$ROOT/patches/wake-without-aec.patch"

if [ ! -f "$IDF_ACTIVATE" ]; then
  echo "ESP-IDF activate script not found: $IDF_ACTIVATE" >&2
  exit 1
fi

if [ ! -d "$SRC/.git" ]; then
  mkdir -p "$ROOT/.src"
  git clone --depth 1 --branch "$XIAOZHI_TAG" https://github.com/78/xiaozhi-esp32.git "$SRC"
fi

cd "$SRC"
if ! grep -q "Keep AEC off while only the wake word is listening" main/audio/engines/afe_audio_engine.cc; then
  git apply "$PATCH"
fi

# shellcheck disable=SC1090
. "$IDF_ACTIVATE"

if [ ! -f sdkconfig ]; then
  python scripts/build.py "$BOARD" --language "$LANGUAGE"
fi
if ! grep -q 'CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_AUDIO_BOARD=y' sdkconfig; then
  echo "Build tree is not the Waveshare ESP32-S3 audio board. Remove speakers/.src and run again." >&2
  exit 1
fi

python3 - "$WAKE_PHRASE" "$WAKE_DISPLAY" "$WAKE_THRESHOLD" "$LANGUAGE" << 'PY'
import pathlib, sys
phrase, display, threshold, language = sys.argv[1:]
lang_symbol = "LANGUAGE_" + language.replace("-", "_").upper()
path = pathlib.Path("sdkconfig")
text = path.read_text()
lines = text.splitlines()

def unset(name, rows):
    out = []
    found = False
    for row in rows:
        if row.startswith(f"CONFIG_{name}=") or row.startswith(f"# CONFIG_{name} is not set"):
            out.append(f"# CONFIG_{name} is not set")
            found = True
        else:
            out.append(row)
    if not found:
        out.append(f"# CONFIG_{name} is not set")
    return out

def set_raw(name, value, rows):
    out = []
    found = False
    for row in rows:
        if row.startswith(f"CONFIG_{name}=") or row.startswith(f"# CONFIG_{name} is not set"):
            out.append(f"CONFIG_{name}={value}")
            found = True
        else:
            out.append(row)
    if not found:
        out.append(f"CONFIG_{name}={value}")
    return out

wake_off = [
    "USE_ESP_WAKE_WORD",
    "USE_AFE_WAKE_WORD",
    "WAKE_WORD_DISABLED",
]
for name in wake_off:
    lines = unset(name, lines)
lines = set_raw("USE_CUSTOM_WAKE_WORD", "y", lines)
lines = set_raw("CUSTOM_WAKE_WORD", f'"{phrase}"', lines)
lines = set_raw("CUSTOM_WAKE_WORD_DISPLAY", f'"{display}"', lines)
lines = set_raw("CUSTOM_WAKE_WORD_THRESHOLD", str(int(threshold)), lines)
lines = set_raw("SR_MN_EN_MULTINET7_QUANT", "y", lines)
lines = set_raw(lang_symbol, "y", lines)

rewritten = []
for row in lines:
    if row.startswith("CONFIG_SR_MN_EN_") and "MULTINET7_QUANT" not in row and row.endswith("=y"):
        key = row.split("=", 1)[0][len("CONFIG_"):]
        rewritten.append(f"# CONFIG_{key} is not set")
    elif row.startswith("CONFIG_SR_WN_") and row.endswith("=y"):
        key = row.split("=", 1)[0][len("CONFIG_"):]
        rewritten.append(f"# CONFIG_{key} is not set")
    elif row.startswith("CONFIG_LANGUAGE_") and row.endswith("=y") and not row.startswith(f"CONFIG_{lang_symbol}="):
        key = row.split("=", 1)[0][len("CONFIG_"):]
        rewritten.append(f"# CONFIG_{key} is not set")
    else:
        rewritten.append(row)
path.write_text("\n".join(rewritten) + "\n")
print(f"sdkconfig wake word: {phrase!r} threshold {threshold}")
PY

idf.py build

BUILD="$SRC/build"
OUT="$ROOT/firmware/$SPEAKER_ID"
mkdir -p "$OUT"
cp "$BUILD/bootloader/bootloader.bin" "$OUT/bootloader.bin"
cp "$BUILD/partition_table/partition-table.bin" "$OUT/partition-table.bin"
cp "$BUILD/ota_data_initial.bin" "$OUT/ota_data_initial.bin"
cp "$BUILD/xiaozhi.bin" "$OUT/xiaozhi.bin"
cp "$BUILD/generated_assets.bin" "$OUT/assets.bin"
cat > "$OUT/flash_args" << EOF
--flash-mode dio --flash-freq 80m --flash-size 16MB
0x0 bootloader.bin
0x8000 partition-table.bin
0xd000 ota_data_initial.bin
0x20000 xiaozhi.bin
0x800000 assets.bin
EOF

"$ROOT/flash.sh" "$SPEAKER_ID" "$PORT"
echo "Image saved in $OUT"
echo "Say: $WAKE_DISPLAY"
