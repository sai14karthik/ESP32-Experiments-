#!/bin/sh
# Moved: Wi-Fi AirPlay speaker package lives in ../wifi_speaker/
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
exec "$ROOT/wifi_speaker/flash.sh" "$@"
