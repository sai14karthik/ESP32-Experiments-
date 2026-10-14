# csi_between_devices (Espressif §4.2)

**Get CSI between devices** — matches [esp-csi README §4.2](../../../README.md#42-get-csi-between-devices) and diagram [`docs/_static/get_device_csi.png`](../../../docs/_static/get_device_csi.png).

> Both STAs join the same AP and ping the gateway. The **sense** board measures CSI from frames **transmitted by the peer STA** (promiscuous + MAC filter `1a:00:00:00:00:0a`), *not* from the router’s ping reply (that is §4.1 `csi_recv_router`).

## Roles

| Role | `sdkconfig` | What it does |
|------|-------------|--------------|
| **PEER** | `sdkconfig.defaults.peer` → `CONFIG_CSI_BETWEEN_ROLE_PEER` | STA MAC forced to `1a:00:00:00:00:0a`, joins AP, pings gateway (traffic source) |
| **SENSE** | `sdkconfig.defaults.sense` → `CONFIG_CSI_BETWEEN_ROLE_SENSE` | Joins AP, pings gateway, CSI callback filtered to peer MAC only |

Firmware: `main/app_main.c` (`wifi_csi_init_from_peer`, `apply_peer_mac`).

## Repo helpers (from repository root)

```bash
# 1st port = PEER, 2nd = SENSE
./scripts/flash_csi_between.sh 'LabHealthSecurePSK' 'PASSWORD' \
  /dev/cu.usbmodem101 /dev/cu.usbserial-110

# Or SSID/PASS from csi_presence_c5/configs/wifi.env
./scripts/flash_csi_between.sh

./scripts/monitor_csi_between.sh /dev/cu.usbserial-110   # sense only
./scripts/plot_csi.sh /dev/cu.usbserial-110
```

See [CSI_METHODS.md](../../../../docs/CSI_METHODS.md).

## Manual flash

```bash
# Peer
idf.py -B build-peer -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;sdkconfig.defaults.peer" set-target esp32c5
idf.py -B build-peer build flash -p PORT_PEER

# Sense
idf.py -B build-sense -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;sdkconfig.defaults.sense" set-target esp32c5
idf.py -B build-sense build flash -p PORT_SENSE
```

Wi‑Fi: copy `sdkconfig.defaults.local.example` → `sdkconfig.defaults.local` (gitignored) and set SSID/password.

**Expect on sense:** `CSI_DATA,…,1a:00:00:00:00:0a,…`  
Console baud: **115200**.

## Wall power → Mini (same collect script as §4.1)

Sense enables `CONFIG_CSI_TCP_*` and forwards `CSI_DATA` to the Mini (`10.128.93.13:9055` by default). Peer is radio-only (no TCP).

```bash
# After flash_csi_between.sh: unplug both → wall power
cd csi_presence_c5
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --session between_01
# expect 1 TCP client (sense); MAC 1a:00:00:00:00:0a
```

| | This example (§4.2) | Multi-RX §4.1 |
|--|---------------------|---------------|
| Firmware | `csi_between_devices` | `csi_recv_router` |
| CSI from | peer STA | router |
| TCP clients on Mini | **1** (sense) | N (each RX) |
