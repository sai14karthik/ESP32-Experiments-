# CSI presence (2× ESP32-C5 + LabPSK + CV labels)

**Binary presence only:** empty vs occupied.  
**CV = ground truth during collection.** Live inference is CSI-only.

```
LabPSK AP
   ↑ pings / traffic
C5 TX (STA)          C5 RX (STA + CSI)
   USB (optional)       USB CSI_DATA ──► this Mac (collector)
Camera / webcam ───────────────────────► CV label (0/1)
```

Detected ports on this Mac (update if they change):

| Role | Serial device |
|------|----------------|
| TX (default) | `/dev/cu.usbmodem101` |
| RX (default) | `/dev/cu.usbmodem1101` |

Serial baud for esp-csi get-started: **115200**.

## Folder map

```
csi_presence_c5/
  README.md
  pyproject.toml
  configs/                 # room, ports, wifi (no secrets in git)
  data/                    # sessions (gitignored)
  models/                  # trained presence models (gitignored except .gitkeep)
  firmware/                # ESP-IDF flash helpers + LabPSK overlays
  scripts/                 # list_ports, flash_tx, flash_rx, collect, train, live
  src/csi_presence/
    csi_parse.py           # CSI_DATA line parser
    features.py            # window → feature vector
    data_collector/        # serial CSI + session writer
    cv_ground_truth/       # webcam/person → occupied 0/1
    train/                 # train binary classifier
    live/                  # CSI-only live presence
```

## Quick start

```bash
cd csi_presence_c5
uv sync
./scripts/list_ports.sh

# Flash both C5s (needs ESP-IDF ≥ 5.4 on PATH via export.sh)
./scripts/flash_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101

# One collect run: camera auto-labels empty↔occupied as you leave/enter the frame
./scripts/collect_session.sh --rx /dev/cu.usbmodem1101 --camera 0 --seconds 120

./scripts/train_presence.sh
./scripts/live_presence.sh --rx /dev/cu.usbmodem1101
```

During collect, spend ~half the time out of view (EMPTY) and half in view (OCCUPIED). The preview window flips the label automatically — no second command.

## Firmware

Default path: **ESP-NOW TX/RX pair** (`csi_send` + `csi_recv`) — no router required for the CSI link; works in the LabPSK RF room.

Optional LabPSK router-ping RX: `firmware/scripts/flash_rx_router.sh` + `configs/wifi.env`.

Details: `firmware/README.md`.

## Scope

| In | Out |
|----|-----|
| empty / occupied | person count |
| CV labels for training | bed / fall / breathing |
| LabPSK + 2× C5 | hospital multi-room yet |
