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
**Plotter:** `./plot_csi.sh [port]` (not together with monitor on the same port)  
**List ports:** `ls /dev/cu.usb*`

**Note on 4.3:** Full Espressif §4.3 is a multi-channel broadcaster. Get-started uses fixed channel **11**, ESP‑NOW, sender MAC `1a:00:00:00:00:00`.

---

## 4.1 — Get router CSI (one C5 + AP)

ESP joins the AP, pings the gateway, CSI from the **router’s ping reply** (filter = AP BSSID).

| | |
|---|---|
| Boards | 1× |
| Firmware | `csi_recv_router` |
| Wi‑Fi | Required |

### Examples

```bash
# Phone hotspot
./scripts/set_csi_wifi.sh 'SaiPhone' '123456789'
./scripts/set_csi_wifi.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101

# Other APs (use single quotes if password has ! @ etc.)
./scripts/set_csi_wifi.sh 'SpectrumSetup-EB9C' 'unitedvideo788'
./scripts/set_csi_wifi.sh 'YourSSID' 'YourPassword'

# Monitor then plot (quit monitor with Ctrl+] first)
./monitor_csi.sh /dev/cu.usbmodem101
./plot_csi.sh /dev/cu.usbmodem101
```

Look for: `got ip:…` then `CSI_DATA,…` (MAC = AP BSSID).

---

## 4.2 — Get CSI between devices (two C5s + AP)

Both join the **same** AP and ping. Sense board measures CSI from the **peer** STA (`1a:00:00:00:00:0a`), not the router.

| | |
|---|---|
| Boards | 2× |
| Firmware | `csi_between_devices` |
| Peer port | Traffic source (MAC `1a:00:00:00:00:0a`) |
| Sense port | Plot this one |

### Examples

```bash
# Flash both (peer first, sense second)
./scripts/flash_csi_between.sh 'SaiPhone' '123456789'
./scripts/flash_csi_between.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101 /dev/cu.usbmodem2101

./scripts/flash_csi_between.sh 'SpectrumSetup-EB9C' 'unitedvideo788' \
  /dev/cu.usbmodem101 /dev/cu.usbmodem2101

./scripts/flash_csi_between.sh 'YourSSID' 'YourPassword' \
  /dev/cu.usbmodem101 /dev/cu.usbmodem2101

# Reuse Wi‑Fi from csi_recv_router/sdkconfig.defaults.local; auto-detect ports
./scripts/flash_csi_between.sh

# Plot sense board only
./plot_csi.sh /dev/cu.usbmodem2101
```

Look for: `CSI_DATA,…,1a:00:00:00:00:0a,…`

---

## 4.3 — Get CSI from a specific sender (two C5s, ESP‑NOW)

No AP/SSID. `csi_send` broadcasts; `csi_recv` filters sender MAC `1a:00:00:00:00:00`. Channel 11, ~100 Hz. Place boards **> ~1 m** apart.

| | |
|---|---|
| Boards | 2× |
| Firmware | `csi_send` + `csi_recv` |
| Send port | First arg |
| Recv port | Second arg — plot this one |

### Examples

```bash
# Auto-pick two ports
./scripts/flash_csi_pair.sh

# Explicit ports (send, then recv)
./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101

# Plot receiver only
./plot_csi.sh /dev/cu.usbmodem2101
```

Look for: `CSI_DATA,…,1a:00:00:00:00:00,…`  
(`set_csi_wifi.sh` / `monitor_csi.sh` are **4.1 only**.)

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
| `./monitor_csi.sh` | 4.1 | IDF serial monitor |
| `./scripts/flash_csi_between.sh` | 4.2 | Flash peer + sense |
| `./scripts/flash_csi_pair.sh` | 4.3 | Flash send + recv |
| `./plot_csi.sh` | 4.1 / 4.2 / 4.3 | Live CSI plot (sense/recv/router port) |
| `./scripts/setup_python.sh` | — | Create `.venv` for the plotter |
