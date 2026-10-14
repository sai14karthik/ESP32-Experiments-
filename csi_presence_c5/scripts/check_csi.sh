#!/usr/bin/env bash
# Alias: full check is check_esp.sh (Sense + CSI). This runs CSI section via same tool.
exec "$(cd "$(dirname "$0")" && pwd)/check_esp.sh" "$@"
