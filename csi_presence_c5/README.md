# CSI presence data collection (Mac Mini + wall-powered ESP32-C5)

**Collect only** — CSI + CV ground truth (empty vs occupied).  
**Target host:** Mac Mini. Boards run on **wall power**; USB is for flash only.

```
LabPSK AP
   ↑ CSI (method 4.1 ping)
C5 #1 (wall) ──TCP :9055──┐
C5 #2 (wall) ──TCP :9055──┼──► Mac Mini collect + YOLO26-pose GT
Webcam/camera on Mini ────┘
```

## Once: flash boards (USB), then unplug to wall power

On a machine with ESP-IDF (this Mac or Mini):

```bash
cd csi_presence_c5
cp configs/wifi.env.example configs/wifi.env
# edit WIFI_SSID / WIFI_PASSWORD / CSI_TCP_HOST=<Mini LabPSK IP, e.g. 10.128.93.13>

./scripts/flash_wall_tcp.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101
```

Then unplug USB → wall power. Boards join LabPSK and open TCP to Mini `:9055`.

## Collect on Mac Mini

```bash
cd csi_presence_c5
uv sync

# Sense cam (.15) + CSI C5s (USB / TCP :9055)
./scripts/check_esp.sh

# denser defaults: hop 0.25s (~4 windows/s), 300s session (configs/collect.toml)
./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense
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
