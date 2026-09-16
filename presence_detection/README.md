# Presence detection (ESP32-C5 CSI)

Home for **presence / empty-vs-occupied** code. Capture still uses the Mini TCP ingest; models and detectors live here.

```
LabPSK AP --CSI--> C5 #1 ──TCP :9055──┐
           --CSI--> C5 #2 ──TCP :9055──┼──► Mac Mini (csi_pipeline_new) → Postgres
           --CSI--> C5 #3 ──TCP :9055──┘
                                              │
                                              ▼
                                    presence_detection/  (train / live / eval)
```

MediaMTX / XIAO camera stay **separate**.

## Lab facts (room 207)

| | |
|---|---|
| Method | **4.1** (router CSI) |
| Receivers | **3×** wall-powered ESP32-C5 (`csi_recv_router`) |
| Mini IP | `10.128.93.23` |
| Ingest | TCP `:9055` → `csi_sessions` / `csi_samples` (`source_id` = client IP) |

## Capture (unchanged)

On Mini — do **not** reimplement ingest here:

```bash
cd csi_pipeline_new
./run_multi_ingest.sh --label empty_01
./run_multi_ingest.sh --label occupied_01
./count_csi_clients.sh          # expect ~3 live forwarders
```

Interleave empty / occupied labels so the model does not learn session geometry. Details: [`csi_pipeline_new/MAC_MINI.md`](../csi_pipeline_new/MAC_MINI.md).

## This folder

| Path | Role |
|------|------|
| `README.md` | This runbook |
| `models/` | Trained bundles (gitignored weights OK) |
| `exports/` | Training CSV / feature dumps |
| `src/` | Presence train / live / eval code (new work) |

Existing reference trainers/detectors still in [`csi_pipeline_new/`](../csi_pipeline_new/) (`train_object_detector.py`, `detect_live.py`, …). New presence work lands under `src/` here; import or thin-wrap capture helpers from `csi_pipeline_new` as needed.

## Next steps

1. Confirm 3 C5s fan-in (`count_csi_clients.sh`).
2. Collect interleaved labeled sessions into Postgres.
3. Export → train presence model under `src/` → `models/`.
4. Live infer on Mini (TCP or recent DB windows).
