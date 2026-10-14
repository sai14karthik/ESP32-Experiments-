# Firmware — 2× ESP32-C5 on LabPSK

Host Python lives in `../src`. Boards get CSI traffic from **esp-csi** examples (IDF ≥ 5.4).

## Roles

| Board | Flash | Expected USB |
|-------|--------|--------------|
| **TX** | `csi_send` (ESP-NOW traffic generator) | mostly status logs |
| **RX** | `csi_recv` | continuous `CSI_DATA,...` lines @ 115200 |

Flash scripts auto-detect chip (C5 default on this Mac; C6 also supported).

## One-time setup

```bash
# ESP-IDF 5.4+ with ESP32-C5 support
. $HOME/esp/esp-idf/export.sh   # or ~/.espressif/tools/activate_idf_*.sh

# Wi-Fi secrets (gitignored) — only needed for flash_rx_router
cp ../configs/wifi.env.example ../configs/wifi.env
# edit WIFI_SSID / WIFI_PASSWORD

# From repo root (camera_module)
export CSI_PRESENCE_ROOT="$(pwd)/csi_presence_c5"
export ESP_CSI="$(pwd)/esp-csi"
```

## Flash TX → `/dev/cu.usbmodem101` (default)

```bash
./scripts/flash_tx.sh /dev/cu.usbmodem101
```

Uses `esp-csi/examples/get-started/csi_send` with target `esp32c5`.

## Flash RX → `/dev/cu.usbmodem1101` (default)

```bash
./scripts/flash_rx.sh /dev/cu.usbmodem1101
```

Uses `esp-csi/examples/get-started/csi_recv` with target `esp32c5`.

## Verify RX

```bash
screen /dev/cu.usbmodem1101 115200
# look for: CSI_DATA,...
```

Then collect on the host:

```bash
../scripts/collect_session.sh --rx /dev/cu.usbmodem1101 --baud 115200
```

## Notes

- Swap port args if USB order differs (`../scripts/list_ports.sh`).
- Update `../configs/ports.toml` `[roles]` so `rx` points at the board printing `CSI_DATA`.
- SoftAP/ESP-NOW TX↔RX does **not** require LabPSK for CSI itself; LabPSK is for shared lab RF environment / optional STA join.
- For router-ping CSI (method 4.1 style), flash RX with `csi_recv_router` instead — see `flash_rx_router.sh`.
