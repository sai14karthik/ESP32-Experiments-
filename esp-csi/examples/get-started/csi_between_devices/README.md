# csi_between_devices (Espressif §4.2)

CSI **between two STAs** on the same AP — matches [esp-csi README §4.2](../../../README.md#42-get-csi-between-devices).

Both boards join Wi‑Fi and ping the gateway. The **sense** board enables promiscuous mode and filters CSI by the **peer** board’s fixed MAC (`1a:00:00:00:00:0a`).

## Roles

| Role | What it does |
|------|----------------|
| **PEER** | Sets STA MAC to `1a:00:00:00:00:0a`, joins AP, pings gateway (traffic source) |
| **SENSE** | Joins AP, pings gateway, CSI from peer MAC only (plot this board) |

## Repo helpers (from repository root)

```bash
./scripts/flash_csi_between.sh 'SSID' 'PASSWORD' [peer-port] [sense-port]
./plot_csi.sh <sense-port>
```

See also [CSI_METHODS.md](../../../../CSI_METHODS.md) at the repo root.

## Manual flash

```bash
# Peer
idf.py -B build-peer -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;sdkconfig.defaults.peer" set-target esp32c5
idf.py -B build-peer build flash -p PORT_PEER

# Sense
idf.py -B build-sense -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local;sdkconfig.defaults.sense" set-target esp32c5
idf.py -B build-sense build flash -p PORT_SENSE
```

Wi‑Fi: copy `sdkconfig.defaults.local.example` → `sdkconfig.defaults.local` and set SSID/password.

Console baud: **115200**.
