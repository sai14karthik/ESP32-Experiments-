# Presence detection (ESP32-C5 CSI)

**Front door:** `./run_presence.sh` — capture stays in `csi_pipeline_new/`;
models and exports live here. **N boards** = distinct `source_id`s (not hardcoded
to 3). Retrain if you add/remove RXs **or** a board gets a new DHCP IP.

```
LabPSK AP (TX) --CSI--> N× C5 RX --TCP :9055--> Mini
                                              ├─ Postgres (capture)
                                              └─ presence_detection/models (train/live)
```

**:9055 is exclusive** — capture, calibrate-live, live, and gui cannot share the
port. Stop the current owner before starting the next.

## Daily loop (Mini)

```bash
cd presence_detection

./run_presence.sh clients                 # expect N boards (needs a listener)
./run_presence.sh capture empty_01        # ~2 min, Ctrl+C
./run_presence.sh capture occupied_01
# interleave more empty_* / occupied_* …

./run_presence.sh train                   # fuse N RXs → models/object_detector.joblib
./run_presence.sh status                  # must show rx_fusion=concat, N=your boards

# Leave room EMPTY; stop ingest (frees :9055):
./run_presence.sh calibrate-live          # threshold from live TCP (not old CSV)
./run_presence.sh live                    # continuous P(presence)
# or: ./run_presence.sh gui               # PyQt dashboard

./run_presence.sh eval
```

### Success criteria

| Check | Expect |
|-------|--------|
| `status` | `rx_fusion=concat`, N = powered boards, sources = their IPs |
| Empty room | mostly EMPTY / low P(presence) |
| Person in RF path | PRESENCE / P above threshold |
| Live line | `rx=N/N` (not stuck buffering) |
| Logs | no `ignoring source_id=` warnings |

If live is still wrong after `calibrate-live` (empty median P already high), retrain:

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
| `run_presence.sh` | Capture / train / calibrate-live / live / gui / eval |
| `models/` | `object_detector.joblib`, `site_calibration.joblib` |
| `exports/` | Synced `training_packets.csv` |
| `src/` | Paths + status helper |
| `../csi_pipeline_new/` | Ingest, features, TCP fan-in, trainers |

## N-board behavior

| Step | How N is chosen |
|------|-----------------|
| Capture | Every C5 that connects to `:9055` |
| Train | Distinct `source_id` → width `N × per-RX` (`--rx-min all` by default) |
| Calibrate / live | Bundle `rx_sources_order` (length N); live `--rx-min all` by default |

`calibrate` (CSV empty rows) is a fallback. Prefer **`calibrate-live`** so the
threshold matches the room right now.

```bash
./run_presence.sh train --rx-min all
./run_presence.sh train --rx-min 2
./run_presence.sh live --rx-min all
./run_presence.sh live --rx-min 2
./run_presence.sh calibrate-live --seconds 120 --fpr 0.02
./run_presence.sh live --quiet
```
