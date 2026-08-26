# CSI collection methods (ESP32-C5)

Aligned with Espressif’s **“How to get CSI”** in [`esp-csi/README.md`](esp-csi/README.md).

| §4.1 Router | §4.2 Between devices | §4.3 Specific / broadcast |
|-------------|----------------------|---------------------------|
| ![4.1](esp-csi/docs/_static/get_router_csi.png) | ![4.2](esp-csi/docs/_static/get_device_csi.png) | ![4.3](esp-csi/docs/_static/get_broadcast_csi.png) |

| § | Name | AP needed? | Example path | Helper |
|---|------|------------|--------------|--------|
| **4.1** | Router CSI | Yes | `esp-csi/examples/get-started/csi_recv_router` | `set_csi_wifi.sh` · `monitor_csi.sh` |
| **4.2** | Between devices | Yes | `esp-csi/examples/get-started/csi_between_devices` | `flash_csi_between.sh` |
| **4.3** | Specific sender | No (CSI link) | `esp-csi/examples/get-started/csi_send` + `csi_recv` | `flash_csi_pair.sh` |

**Hardware:** ESP32-C5-KITC-A · `/dev/cu.usbmodem*` or `/dev/cu.usbserial*` · baud **115200**  
**List ports:** `ls /dev/cu.usb*`  
**Rule:** do **not** run terminal monitor and `./plot_csi.sh` on the **same** port at the same time.

**Note on 4.3:** Full Espressif §4.3 is a multi-channel broadcaster. Get-started uses fixed channel **11**, ESP‑NOW, sender MAC `1a:00:00:00:00:00`.

### Terminal vs plot (all methods)

| View | How |
|------|-----|
| **Terminal** (raw `CSI_DATA` lines) | IDF `idf.py … monitor` or `screen` — see each method below |
| **Plot** (optional) | `./plot_csi.sh [port]` after quitting the monitor |

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
./monitor_csi.sh /dev/cu.usbmodem101

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
./plot_csi.sh /dev/cu.usbmodem101
```

---

## 4.2 — Get CSI between devices (two C5s + AP)

Both join the **same** AP and ping. Sense board measures CSI from the **peer** STA (`1a:00:00:00:00:0a`), not the router.

| | |
|---|---|
| Boards | 2× |
| Firmware | `csi_between_devices` |
| Peer port | Traffic only (usually no CSI lines) |
| Sense port | **Terminal / plot this one** |

### Flash examples

```bash
./scripts/flash_csi_between.sh 'SaiPhone' '123456789'
./scripts/flash_csi_between.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101 /dev/cu.usbmodem2101

./scripts/flash_csi_between.sh 'SpectrumSetup-EB9C' 'unitedvideo788' \
  /dev/cu.usbmodem101 /dev/cu.usbmodem2101

./scripts/flash_csi_between.sh 'YourSSID' 'YourPassword' \
  /dev/cu.usbmodem101 /dev/cu.usbmodem2101

# Reuse Wi‑Fi from csi_recv_router/sdkconfig.defaults.local; auto-detect ports
./scripts/flash_csi_between.sh
```

### See output in terminal (sense board)

```bash
source "$HOME/.espressif/tools/activate_idf_v6.0.2.sh"
cd esp-csi/examples/get-started/csi_between_devices
idf.py -B build-sense -p /dev/cu.usbmodem2101 monitor

# Or plain serial
screen /dev/cu.usbmodem2101 115200
```

(`./monitor_csi.sh` is **4.1 only** — do not use it for 4.2.)

**Expect:** `CSI_DATA,…,1a:00:00:00:00:0a,…`

### Optional plot

```bash
./plot_csi.sh /dev/cu.usbmodem2101
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

(`./monitor_csi.sh` is **4.1 only** — do not use it for 4.3.)

**Expect:** `CSI_DATA,…,1a:00:00:00:00:00,…`

### Optional plot

```bash
./plot_csi.sh /dev/cu.usbmodem2101
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
| `./monitor_csi.sh` | **4.1 only** | IDF serial monitor (`csi_recv_router`) |
| `./scripts/flash_csi_between.sh` | 4.2 | Flash peer + sense |
| `./scripts/flash_csi_pair.sh` | 4.3 | Flash send + recv |
| `idf.py -p PORT monitor` | 4.2 / 4.3 | Terminal output (from correct example dir) |
| `screen PORT 115200` | all | Terminal output without IDF |
| `./plot_csi.sh` | all | Live CSI plot (optional) |
| `./scripts/setup_python.sh` | — | Create `.venv` for the plotter |
source "$HOME/.espressif/tools/activate_idf_v6.0.2.sh"
