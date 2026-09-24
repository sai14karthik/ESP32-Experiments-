# Presence detection (ESP32-C5 CSI)

**Front door:** `./run_presence.sh` — capture stays in `csi_pipeline_new/`;
models and exports live here. **N boards** = distinct `source_id`s (not hardcoded
to 3). Retrain if you add/remove RXs **or** a board gets a new DHCP IP.

```
LabPSK AP (TX) --CSI--> N× C5 RX --TCP :9055--> Mini
                                              ├─ Postgres (capture)
                                              └─ presence_detection/models (train/live)
```

**:9055 is exclusive** — capture, calibrate-live, live, gui, and web cannot share the
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
# or: ./run_presence.sh gui               # PyQt on Mini
# or: ./run_presence.sh web               # phone on LabPSK → http://10.128.93.13:8765
./run_presence.sh eval
```

## Phone UI (any iOS / Android browser)

Mini serves a mobile page while it runs live CSI detect:

```bash
# On Mini — stop ingest / live / gui first (:9055 exclusive)
./run_presence.sh web
```

| | |
|--|--|
| **URL** | http://10.128.93.13:8765 |
| **Phone Wi‑Fi** | **LabPSK** (same LAN as Mini — devices can reach each other) |
| **Shows** | EMPTY / PRESENCE, score line, live ESP list |
| **Room** | Default **Room 207** (`--room "Room 207"`) |

Phone does **not** talk to the ESP boards; it only loads the Mini web page.
C5s still forward CSI to Mini on `:9055` as usual.

```bash
./run_presence.sh test-web   # web hub / HTTP / TCP status / wiring
./run_presence.sh test-e2e   # full A–Z: train → cal → live 1..N → web → suites
./run_presence.sh web --http-port 8765   # optional port override
```

### Success criteria

| Check | Expect |
|-------|--------|
| `status` | `rx_fusion=concat` (N≥2) or single-RX if trained with 1 board |
| Empty room | mostly EMPTY / low P(presence) |
| Person in RF path | PRESENCE / P above threshold |
| Live line | `rx=k/N` for any k in 1..N (not stuck forever) |
| Logs | no stuck buffering when boards are up |

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
| `run_presence.sh` | Capture / train / calibrate-live / live / gui / web / eval |
| `models/` | `object_detector.joblib`, `site_calibration.joblib` |
| `exports/` | Synced `training_packets.csv` |
| `src/` | Paths + status helper |
| `../csi_pipeline_new/` | Ingest, features, TCP fan-in, trainers |

## N-board behavior (fault-tolerant: 1 … max N)

| Boards | Train | Live |
|--------|-------|------|
| **1** | Single-RX model | Works on `:9055` with that one board |
| **2…N** | Fused concat (`N ×` features) | Works with **any k in 1..N** live; missing zero-padded; new IPs hot-plug |
| **N+1** | Retrain to expand | Extra board ignored until retrain |

No hardcoded max N — whatever distinct `source_id`s appear at train time.

```bash
./run_presence.sh train                 # default --rx-min 1
./run_presence.sh train --rx-min all    # stricter: only full-N bins
./run_presence.sh live                  # default auto (≥1 board)
./run_presence.sh live --rx-min all     # require every trained board
./run_presence.sh live --rx-min 2
./run_presence.sh calibrate-live --seconds 120 --fpr 0.02
./run_presence.sh live --quiet
./run_presence.sh web                   # LabPSK phone → http://10.128.93.13:8765
./run_presence.sh web --http-port 8765
./run_presence.sh test-web              # no-hardware web tests
./run_presence.sh test-e2e              # full A–Z pipeline proof
```
