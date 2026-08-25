# Shared helpers for CSI shell wrappers (sourced, not executed).
# shellcheck shell=bash

pick_usb_serial() {
  if [[ -n "${1:-}" ]]; then
    printf '%s\n' "$1"
    return 0
  fi

  local p
  shopt -s nullglob

  # macOS CH340/CP2102 first (C5), then CDC (XIAO), then Linux USB-UART.
  for p in \
      /dev/cu.usbserial* \
      /dev/cu.wchusbserial* \
      /dev/cu.SLAB_USBtoUART \
      /dev/cu.usbmodem* \
      /dev/ttyUSB* \
      /dev/ttyACM*; do
    [[ -e "$p" ]] || continue
    printf '%s\n' "$p"
    return 0
  done

  return 1
}

find_idf_activate() {
  if [[ -n "${IDF_ACTIVATE:-}" && -f "${IDF_ACTIVATE}" ]]; then
    printf '%s\n' "$IDF_ACTIVATE"
    return 0
  fi

  local f matches=()
  shopt -s nullglob
  matches=("$HOME"/.espressif/tools/activate_idf_*.sh)
  for f in "${matches[@]}"; do
    [[ "$f" == *v6.0.2* ]] || continue
    printf '%s\n' "$f"
    return 0
  done
  if ((${#matches[@]} > 0)); then
    printf '%s\n' "${matches[$((${#matches[@]} - 1))]}"
    return 0
  fi

  for f in \
      "$HOME/esp/esp-idf/export.sh" \
      "$HOME/esp/esp-idf/export.fish" \
      "${IDF_PATH:+$IDF_PATH/export.sh}"; do
    [[ -n "$f" && -f "$f" ]] || continue
    printf '%s\n' "$f"
    return 0
  done

  return 1
}
