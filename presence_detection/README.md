# Presence detection (ESP32-C5 CSI)

**Front door:** `./run_presence.sh` — capture stays in `csi_pipeline_new/`;
models and exports live here. **N boards** = distinct `source_id`s (not hardcoded
to 3). Retrain if you add/remove RXs.

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
# Leave room EMPTY, stop ingest/live:
./run_presence.sh calibrate-live          # threshold from live TCP (not old CSV)
./run_presence.sh live                    # continuous P(object)

./run_presence.sh eval
./run_presence.sh status                  # shows rx_fusion + N + sources
```

If live is still wrong after `calibrate-live` (empty median P already high), retrain with fresh captures:

```bash
./run_presence.sh capture empty_now
./run_presence.sh capture occupied_now
./run_presence.sh train
./run_presence.sh calibrate-live
./run_presence.sh live
```

## Continuous improvement

1. When live is wrong, immediately `./run_presence.sh capture empty_miss_…` or `occupied_miss_…`
2. `./run_presence.sh train` → `calibrate-live` → `live` again  
3. Watch **OOF / grouped bal_acc** via `./run_presence.sh eval` — want stable or rising

## Layout

| Path | Role |
|------|------|
| `run_presence.sh` | Capture / train / calibrate-live / live / eval |
| `models/` | `object_detector.joblib`, `site_calibration.joblib` |
| `exports/` | Synced `training_packets.csv` |
| `src/` | Paths + status helper |
| `../csi_pipeline_new/` | Ingest, features, TCP fan-in, trainers |

## N-board behavior

| Step | How N is chosen |
|------|-----------------|
| Capture | Every C5 that connects to `:9055` |
| Train | Distinct `source_id` → width `N × per-RX` |
| Calibrate / live | Bundle `rx_sources_order` (length N); live `--rx-min all` by default |

```bash
./run_presence.sh train --rx-min all
./run_presence.sh train --rx-min 2
./run_presence.sh live --rx-min all
./run_presence.sh live --rx-min 2
./run_presence.sh calibrate-live --seconds 120 --fpr 0.02
./run_presence.sh live --quiet
```
