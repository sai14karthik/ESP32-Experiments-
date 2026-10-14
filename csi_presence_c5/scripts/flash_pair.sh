#!/usr/bin/env bash
exec "$(cd "$(dirname "$0")/.." && pwd)/firmware/scripts/flash_pair.sh" "$@"
