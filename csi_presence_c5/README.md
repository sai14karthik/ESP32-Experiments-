# CSI presence data collection (Mac Mini + wall-powered ESP32-C5)

**Collect only** — CSI + CV ground truth (empty vs occupied).  
**Target host:** Mac Mini.

Espressif methods (see [`docs/CSI_METHODS.md`](../docs/CSI_METHODS.md)):

| Method | Firmware | How CSI is taken | This project |
|--------|----------|------------------|--------------|
| **§4.1** Router CSI | `csi_recv_router` + TCP | Each C5: CSI from **AP** ping reply | **Default** — wall power → Mini `:9055` |
| **§4.2** Between devices | `csi_between_devices` | Sense C5: CSI from **peer** STA MAC | Sense TCP → Mini `:9055` (peer wall-only) |
| **§4.3** Sender+recv | `csi_send` + `csi_recv` | ESP‑NOW | Later |

```
§4.1 (default Mini path)
LabPSK AP
   ↑ CSI (router ping reply)
C5 #1 (wall) ──TCP :9055──┐
C5 #2 (wall) ──TCP :9055──┼──► Mac Mini collect + YOLO26-pose GT
Sense cam (.15 / MediaMTX) ──┘
```

## Flash §4.1 (wall TCP) — default

```bash
cd csi_presence_c5
cp configs/wifi.env.example configs/wifi.env
# edit WIFI_SSID / WIFI_PASSWORD / CSI_TCP_HOST=<Mini LabPSK IP, e.g. 10.128.93.13>

./scripts/flash_wall_tcp.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101
```

Then unplug USB → wall power. Boards join LabPSK and open TCP to Mini `:9055`.

## Flash §4.2 (between devices) — wall power + Mini

From **repo root**. First port = **PEER**, second = **SENSE** (TCP → `CSI_TCP_HOST` in `wifi.env`):

```bash
cd ..   # camera_module root
./scripts/flash_csi_between.sh   # uses configs/wifi.env SSID + CSI_TCP_HOST
# then unplug both → wall power

# On Mini (same collect as 4.1 — opens :9055):
cd csi_presence_c5
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --session between_01
# expect 1 TCP client (sense only); CSI MAC = 1a:00:00:00:00:0a
```

USB debug optional: `./scripts/monitor_csi_between.sh SENSE_PORT`.  
Back to §4.1 multi-RX: `./scripts/flash_wall_tcp.sh`.

## Collect on Mac Mini

```bash
cd csi_presence_c5
uv sync

# Sense cam (.15) + CSI C5s (USB / TCP :9055)
./scripts/check_esp.sh

# denser defaults: hop 0.25s (~4 windows/s), 300s (configs/collect.toml)
# --session TAG names the folder, e.g. 20261014T…_session1
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --session session1
```

Leave/enter the **ESP cam** frame in the same run. Sessions → `data/sessions/`.

ESP `/stream` is usually **one client only**. If MediaMTX already holds `.15`, direct MJPEG times out — use RTSP instead (or stop MediaMTX):

```bash
# Recommended when mediamtx_run.sh is up (default 300s / hop 0.25):
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense

# Shorter / denser overrides:
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --seconds 180 --hop-sec 0.2

# Direct ESP (no other MJPEG client):
./scripts/collect_session.sh --camera http://10.128.93.15:81/stream --seconds 180
```

## Laptop USB debug (optional)

```bash
./scripts/collect_session.sh --rx /dev/cu.usbmodem1101 --camera http://10.128.93.15:81/stream --seconds 60
```

## CV ground truth

Same **YOLO26-pose** presets as `healthcare_pose_activity` (`accurate` → `yolo26m-pose`).  
Labels are presence only: empty(0) / occupied(1).

## Folder map

```
csi_presence_c5/
  configs/           ports, collect, wifi.env.example
  data/sessions/     collected sessions
  firmware/scripts/  flash_wall_tcp, flash_pair (ESP-NOW USB debug)
  scripts/           collect_session, flash_wall_tcp, list_ports
  src/csi_presence/
    data_collector/  serial + TCP :9055 readers
    cv_ground_truth/ YOLO26-pose presence GT
```
