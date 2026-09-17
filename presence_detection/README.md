# Presence detection (ESP32-C5 CSI)

**Front door:** `./run_presence.sh` — capture stays in `csi_pipeline_new/`;
models and exports live here.

```
LabPSK AP (TX) --CSI--> N× C5 RX --TCP :9055--> Mini
                                              ├─ Postgres (capture)
                                              └─ presence_detection/models (train/live)
```

## Daily loop (Mini)

```bash
cd presence_detection

./run_presence.sh clients                 # expect N boards
./run_presence.sh capture empty_01        # ~2 min, Ctrl+C
./run_presence.sh capture occupied_01
# interleave more empty_* / occupied_* …

./run_presence.sh train                   # fuse N RXs → models/object_detector.joblib
./run_presence.sh calibrate               # empty-room threshold → models/site_calibration.joblib
# Ctrl+C any ./run_multi_ingest.sh first (same :9055)
./run_presence.sh live                    # continuous P(object) lines (--fast)
./run_presence.sh live --quiet            # only EMPTY ↔ OBJECT changes

./run_presence.sh eval                    # reprint metrics anytime
./run_presence.sh status                  # fusion N, bal_acc, cal
```

## Continuous improvement

1. When live is wrong, immediately `./run_presence.sh capture empty_miss_…` or `occupied_miss_…`
2. `./run_presence.sh train` → `calibrate` → `live` again  
3. Watch **OOF / grouped bal_acc** via `./run_presence.sh eval` — want stable or rising (~0.70 today)

## Layout

| Path | Role |
|------|------|
| `run_presence.sh` | Capture / train / calibrate / live / eval |
| `models/` | `object_detector.joblib`, `site_calibration.joblib` |
| `exports/` | Synced `training_packets.csv` |
| `src/` | Paths + status helper |
| `../csi_pipeline_new/` | Ingest, features, TCP fan-in, trainers |

## Flags

Train defaults to `--include empty,occupied` and multi-RX `--rx-fusion auto`.
Pass-through examples:

```bash
./run_presence.sh train --rx-min all
./run_presence.sh train --include empty,occupied --source-id 10.128.93.29
./run_presence.sh live --threshold 0.25
```
