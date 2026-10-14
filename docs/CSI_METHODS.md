# CSI collection methods (ESP32-C5)

Aligned with Espressif’s **“How to get CSI”** in [`esp-csi/README.md`](esp-csi/README.md).

| §4.1 Router | §4.2 Between devices | §4.3 Specific / broadcast |
|-------------|----------------------|---------------------------|
| ![4.1](esp-csi/docs/_static/get_router_csi.png) | ![4.2](esp-csi/docs/_static/get_device_csi.png) | ![4.3](esp-csi/docs/_static/get_broadcast_csi.png) |

| § | Name | AP needed? | Example path | Helper |
|---|------|------------|--------------|--------|
| **4.1** | Router CSI | Yes | `esp-csi/examples/get-started/csi_recv_router` | `./scripts/set_csi_wifi.sh` · `./scripts/monitor_csi.sh` |
| **4.2** | Between devices | Yes | `esp-csi/examples/get-started/csi_between_devices` | `flash_csi_between.sh` |
| **4.3** | Specific sender | No (CSI link) | `esp-csi/examples/get-started/csi_send` + `csi_recv` | `flash_csi_pair.sh` |

**Hardware:** ESP32-C5-KITC-A · `/dev/cu.usbmodem*` or `/dev/cu.usbserial*` · baud **115200**  
**List ports:** `ls /dev/cu.usb*`  
**Rule:** do **not** run terminal monitor and `./scripts/plot_csi.sh` on the **same** port at the same time.

**Note on 4.3:** Full Espressif §4.3 is a multi-channel broadcaster. Get-started uses fixed channel **11**, ESP‑NOW, sender MAC `1a:00:00:00:00:00`.

### Terminal vs plot (all methods)

| View | How |
|------|-----|
| **Terminal** (raw `CSI_DATA` lines) | IDF `idf.py … monitor` or `screen` — see each method below |
| **Plot** (optional) | `./scripts/plot_csi.sh [port]` after quitting the monitor |

Quit IDF monitor with **`Ctrl+]`**. Quit `screen` with **`Ctrl+A`**, then **`K`**, then **`Y`**.

---

## 4.1 — Get router CSI (one C5 + AP)

ESP joins the AP, pings the gateway, CSI from the **router’s ping reply** (filter = AP BSSID).

| | |
|---|---|
| Boards | 1× |
| Firmware | `csi_recv_router` |
| Wi‑Fi | Required |
| Terminal port | That one board |

### Flash examples

```bash
./scripts/set_csi_wifi.sh 'SaiPhone' '123456789'
./scripts/set_csi_wifi.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101

./scripts/set_csi_wifi.sh 'SpectrumSetup-EB9C' 'unitedvideo788'
./scripts/set_csi_wifi.sh 'YourSSID' 'YourPassword'
```

### See output in terminal

```bash
# Helper (opens csi_recv_router monitor)
./scripts/monitor_csi.sh /dev/cu.usbmodem101

# Or manually
source "$HOME/.espressif/tools/activate_idf_v6.0.2.sh"
cd esp-csi/examples/get-started/csi_recv_router
idf.py -p /dev/cu.usbmodem101 monitor

# Or plain serial (no IDF)
screen /dev/cu.usbmodem101 115200
```

**Expect:** `got ip:…` then `CSI_DATA,…` (MAC = AP BSSID).

### Optional plot

```bash
# quit terminal monitor first
./scripts/plot_csi.sh /dev/cu.usbmodem101
```

---

## 4.2 — Get CSI between devices (two C5s + AP)

Espressif README: *“ESP32 A and B both send Ping packets to the router, and ESP32 A receives the CSI information carried in the Ping sent by ESP32 B.”*

Diagram in tree: `esp-csi/docs/_static/get_device_csi.png` (looks similar to two router pings; **trust the text + MAC filter**, not the arrow art alone).

| | §4.1 router CSI | **§4.2 between devices** |
|---|---|---|
| Example | `csi_recv_router` | `csi_between_devices` |
| CSI source MAC | AP BSSID | **Peer STA** `1a:00:00:00:00:0a` |
| Boards | 1× (or N× each sensing the AP) | **2×** with roles PEER + SENSE |
| Sense does | CSI on router ping *reply* | Promiscuous CSI on peer’s ping *frames* |
| Mini TCP wall collect | Yes (`flash_wall_tcp.sh`) | Not yet — use **USB sense** (`--rx`) |

| Role | Firmware flag | What it does |
|------|---------------|--------------|
| **PEER** | `CONFIG_CSI_BETWEEN_ROLE_PEER` | Sets STA MAC `1a:00:00:00:00:0a`, joins AP, pings GW (traffic) |
| **SENSE** | `CONFIG_CSI_BETWEEN_ROLE_SENSE` | Joins AP, pings GW, CSI filter = peer MAC only |

### Flash examples

```bash
# Explicit SSID + ports (1st=PEER, 2nd=SENSE)
./scripts/flash_csi_between.sh 'LabHealthSecurePSK' 'PASSWORD' \
  /dev/cu.usbmodem101 /dev/cu.usbserial-110

# SSID/PASS from csi_presence_c5/configs/wifi.env or sdkconfig.defaults.local
./scripts/flash_csi_between.sh
./scripts/flash_csi_between.sh '' '' /dev/cu.usbmodem101 /dev/cu.usbserial-110
```

Flash verifies each image: peer build has `CONFIG_CSI_BETWEEN_ROLE_PEER=y`, sense has `…_SENSE=y`.

### See output in terminal (sense board only)

```bash
./scripts/monitor_csi_between.sh /dev/cu.usbserial-110

# Or plain serial
screen /dev/cu.usbserial-110 115200
```

(`./scripts/monitor_csi.sh` is **4.1 only** — wrong example tree.)

**Expect:** `role=SENSE` then `CSI_DATA,…,1a:00:00:00:00:0a,…`  
Peer log: `role=PEER` / pinging — usually **no** `CSI_DATA`.

### Optional plot / presence GT

```bash
./scripts/plot_csi.sh /dev/cu.usbserial-110

# Presence session (sense USB; peer on wall power OK)
cd csi_presence_c5
./scripts/collect_session.sh --rx /dev/cu.usbserial-110 \
  --camera rtsp://127.0.0.1:8554/cam_sense --session between_01
```

---

## 4.3 — Get CSI from a specific sender (two C5s, ESP‑NOW)

No AP/SSID. `csi_send` broadcasts; `csi_recv` filters sender MAC `1a:00:00:00:00:00`. Channel 11, ~100 Hz. Place boards **> ~1 m** apart.

| | |
|---|---|
| Boards | 2× |
| Firmware | `csi_send` + `csi_recv` |
| Send port | First flash arg (usually quiet / send logs only) |
| Recv port | **Terminal / plot this one** |

### Flash examples

```bash
./scripts/flash_csi_pair.sh
./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101
```

### See output in terminal (recv board)

```bash
cd esp-csi/examples/get-started/csi_recv
idf.py -p /dev/cu.usbmodem2101 monitor

# Or plain serial
screen /dev/cu.usbmodem2101 115200
```

(`./scripts/monitor_csi.sh` is **4.1 only** — do not use it for 4.3.)

**Expect:** `CSI_DATA,…,1a:00:00:00:00:00,…`

### Optional plot

```bash
./scripts/plot_csi.sh /dev/cu.usbmodem2101
```

---

## Switching

| Go to | Example |
|-------|---------|
| **4.1** | `./scripts/set_csi_wifi.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101` |
| **4.2** | `./scripts/flash_csi_between.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101 /dev/cu.usbmodem2101` |
| **4.3** | `./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101` |

One firmware image per board at a time.

---

## Quick reference — all helpers

| Script | Methods | Purpose |
|--------|---------|---------|
| `./scripts/set_csi_wifi.sh` | 4.1 | Set SSID/password, build, flash `csi_recv_router` |
| `./scripts/monitor_csi.sh` | **4.1 only** | IDF serial monitor (`csi_recv_router`) |
| `./scripts/flash_csi_between.sh` | **4.2** | Flash peer + sense (`csi_between_devices`) |
| `./scripts/monitor_csi_between.sh` | **4.2 only** | IDF monitor on **sense** port |
| `./scripts/flash_csi_pair.sh` | 4.3 | Flash send + recv |
| [`csi_pipeline_new/MAC_MINI.md`](csi_pipeline_new/MAC_MINI.md) | 4.1–4.3 | Mac Mini → PostgreSQL ingest |
| `idf.py -B build-sense -p PORT monitor` | 4.2 | From `csi_between_devices` |
| `screen PORT 115200` | all | Terminal without IDF |
| `./scripts/plot_csi.sh` | all | Live CSI plot (optional) |
| `./scripts/setup_python.sh` | — | Create `.venv` for the plotter |

IDF activate (when needed):

```bash
source "$HOME/.espressif/tools/activate_idf_v6.0.2.sh"
```
