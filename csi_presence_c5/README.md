# CSI presence data collection (2× ESP32-C5 + CV labels)

**Collect only for now** — CSI serial + camera ground truth (empty vs occupied).  
Training / live inference are out of scope until we bring them back.

```
C5 TX ──ESP-NOW──► C5 RX ──USB CSI_DATA──► Mac collector
Webcam ──YOLO26-pose (same presets as healthcare_pose_activity)──► empty/occupied
```

**Ground truth = presence only** (someone there / not).  
We load the **same YOLO26-pose model + presets** as `healthcare_pose_activity` (`live_infer.QUALITY`). Pose keypoints are used only to decide “person in frame”; we store `label=0|1`, not sitting/walking/etc.

| `POSE_QUALITY` | Model (from pose pipeline) |
|----------------|----------------------------|
| fast | `yolo26n-pose.pt` |
| balanced | `yolo26s-pose.pt` |
| **accurate (default)** | **`yolo26m-pose.pt`** |
| max | `yolo26l-pose.pt` |

| Role | Serial |
|------|--------|
| TX | `/dev/cu.usbmodem101` |
| RX | `/dev/cu.usbmodem1101` |

Baud: **115200**.

## Folder map

```
csi_presence_c5/
  configs/           ports, collect, wifi.env.example
  data/sessions/     collected sessions (gitignored)
  firmware/          flash TX/RX helpers
  scripts/           list_ports, flash_*, collect, preview_cv
  src/csi_presence/
    data_collector/      serial CSI + session writer
    cv_ground_truth/     YOLO26-pose → empty/occupied
    csi_parse.py
    features.py          window features saved with each label
```

## Collect

```bash
cd csi_presence_c5
uv sync

# Flash once (IDF ≥ 5.4)
./scripts/flash_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101

# One run: leave/enter frame — pose GT flips empty↔occupied automatically
./scripts/collect_session.sh --rx /dev/cu.usbmodem1101 --camera 0 --seconds 120

# Same quality knobs as healthcare pose live:
# POSE_QUALITY=fast ./scripts/collect_session.sh ...
# POSE_MODEL=yolo26l-pose.pt ./scripts/collect_session.sh ...
```

Each session lands in `data/sessions/<timestamp>_auto/`:

- `csi_raw.txt` — raw `CSI_DATA` lines  
- `windows.jsonl` — per-window features + `label` (0/1) + `label_src`  
- `meta.json` — ports, YOLO model, counts  

## Firmware

See `firmware/README.md`. Default = ESP-NOW `csi_send` + `csi_recv`.
