# Presence detection (ESP32-C5 CSI)

Home for **presence / empty-vs-occupied** work. Capture stays in `csi_pipeline_new/`;
train/live will move here over time.

```
LabPSK AP (TX) --CSI--> N× C5 RX --TCP :9055--> Mini Postgres
                                              --> multi-RX-aware train
```

## Capture

```bash
cd csi_pipeline_new
./run_multi_ingest.sh --label empty_01    # ~2 min, Ctrl+C
./run_multi_ingest.sh --label occupied_01
# interleave ~15 each …
```

## Train (multi-RX aware — re-export includes source_id)

On Mini, after syncing this repo:

```bash
cd csi_pipeline_new
./run_detect.sh --train-from-db --include empty,occupied
```

Default **`--rx-fusion auto`**: discovers **N** boards from distinct `source_id`
values (2, 3, 8, … — not hardcoded). Windows are time-binned and **feature-
concatenated in sorted source_id order** (missing board in a bin → zeros).
Train also prints a complementary **OR-vote** score. Overrides:
`--rx-fusion none`, `--rx-fusion concat`, `--rx-min all` (require every board
in each bin), `--rx-min 2` (default; tolerate dropouts).

You should see `RX boards (N=…)`, fused dims ≈ N× single-RX, and **median span > 0**.
If [A] session-grouped bal_acc is still ~0.5, the link geometry needs work —
not the fan-in path. Adding/removing boards later means **retrain** (feature
width follows N).

Optional single-board ablation:

```bash
./run_detect.sh --train-from-db --include empty,occupied --source-id 10.128.93.XX --rx-fusion none
```

## This folder

| Path | Role |
|------|------|
| `models/` | Presence model artifacts (later) |
| `exports/` | Training dumps (later) |
| `src/` | Presence-specific code (later wrappers) |
