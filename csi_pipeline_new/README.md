# CSI Pipeline — Capture to PostgreSQL

ESP32-C5 CSI over USB → parse `CSI_DATA` → store in local PostgreSQL.

**Mac Mini (methods 4.1 / 4.2 / 4.3):** see **[`MAC_MINI.md`](MAC_MINI.md)** — full setup, hardware, ingest commands, export, troubleshooting.

This README is the short reference; [`MAC_MINI.md`](MAC_MINI.md) is the complete Mac Mini runbook.

```
csi_send  --ESP-NOW ch11-->  csi_recv  --USB 115200-->  run_ingest.sh  -->  Postgres (csi DB)
```

| Table | What it holds |
|-------|----------------|
| `csi_sessions` | One row per ingest run (label, method, port, start/end) |
| `csi_samples` | One row per CSI packet (`iq` = 234 ints → 117 I/Q pairs) |

---

## 1. One-time setup (each machine)

```bash
cd csi_pipeline
./setup_mac.sh
```

This installs **PostgreSQL 16** (Homebrew), creates DB `csi`, applies [`schema.sql`](schema.sql), runs `uv sync --group csi`, and writes `.env`.

Requires [Homebrew](https://brew.sh). Apple Silicon path: `/opt/homebrew/...`.

---

## 2. Hardware

See **[`MAC_MINI.md`](MAC_MINI.md)** for per-method wiring (4.1 one board, 4.2 sense board, 4.3 recv + sender on power).

**§4.3 quick check:**

## 3. Capture CSI

```bash
cd csi_pipeline

# Live (auto-picks the recv port)
./run_ingest.sh --method 4.3 --channel 11 --label desk_run1

# Or pin the port
./run_ingest.sh --port /dev/cu.usbmodem1101 --method 4.3 --channel 11 --label desk_run1
```

- **Ctrl+C** stops the run, flushes the last batch, sets `ended_at`.
- Each run → **one** new `csi_sessions` row; packets land in `csi_samples` under that `session_id`.
- Use a clear `--label` every time (`sitting`, `walking`, `baseline`, …).

**Dry-run (no boards):**

```bash
./run_ingest.sh --from-file fixtures/sample_csi_lines.csv --method 4.3 --label dryrun
```

---

## 3b. Real-time object detection (Mac Mini)

Trained on `baseline_*` vs `object_*` exports. Reads the same USB serial stream as ingest (do **not** run both on one port).

```bash
cd ~/Desktop/camera_module
uv sync --group csi          # one-time: psycopg, sklearn, joblib

cd csi_pipeline
./run_detect.sh --train --csv "../sample_data/csi_packets.csv"
./run_detect.sh --eval-csv
./run_detect.sh --ablate     # which feature blocks actually carry signal
./run_detect.sh --self-test  # feature parity + hardware check

# Live — recv USB to Mini, send on power (§4.3)
./run_detect.sh --calibrate  # REQUIRED at each new site — see below
./run_detect.sh --quiet
```

### Calibrate at every site

Two of the numbers in the bundle describe the *training room*, not the
detector: the baseline amplitude profile that every feature is measured
against, and the decision threshold. Carrying them to a new setup does not
work. Measured on two captures where neither had seen the other
(`cross_condition_eval.py`, replayed through the live path):

| | balanced acc | object recall | false alarms |
|---|---|---|---|
| bundle baseline + threshold, unseen site | 0.621 | 1.000 | **0.757** |
| site-calibrated, same data | **0.879** | 0.757 | **0.000** |

Uncalibrated it fires on three quarters of the empty room — it "detects"
everything, which is why the balanced accuracy is near chance despite perfect
recall.

`./run_detect.sh --calibrate` records ~2 minutes of the **empty** room where the
boards will actually live and derives both quantities from it: the baseline as
the per-subcarrier median, the threshold as the quantile of the score on those
windows at the false-positive rate you ask for (`--fpr`, default 0.10). No
labelled object is needed, which is what makes it an install step rather than a
second training run. The result lands in `models/site_calibration.joblib` and
`detect_live.py` picks it up automatically; it is refused if the model is
retrained under it.

On the site the model *was* trained on, calibrating costs a little — 1.000 →
0.952, with the false-alarm rate landing at 0.095, i.e. exactly the 10% you
asked for. Lower `--fpr` if that trade is wrong for you.

**v4 features** (`csi_features.py`):

| Block | Default | Why |
|-------|---------|-----|
| amplitude, gain-normalized | on | each packet divided by its own active-bin mean, so receiver AGC/FFT gain cancels exactly |
| baseline-relative delta + band stats | on | empty-room median as reference |
| phase (detrended, adjacent-diff) | on | CFO/SFO removed by linear detrend across active bins |
| RSSI / AGC / FFT gain | **off** | see below — a single RSSI threshold scores 0.983 on the sample capture |
| flattened window sequence | **off** | was 3420 of 4460 v3 dims against ~2255 windows |

The layout is stored in the model bundle and replayed at inference, so live features match training exactly (`--self-test` asserts bit-equality).

### Read the metrics before trusting them

`--train` prints four tiers and refuses to fake the top one:

- **[A] session-grouped CV** — hold out a whole capture. The only estimate that generalizes. Needs ≥2 sessions per class. **When it is available it also selects the model and tunes the threshold**, and its out-of-fold numbers are what land in the bundle.
- **[B] time-block CV** — hold out a time chunk. Confounded whenever label and session coincide.
- **[C] temporal hold-out** — last 20% of each session. Most optimistic, and used for selection **only as a fallback**, when [A] is impossible; the bundle is then flagged `evaluation_trustworthy=False`.
- **[D] negative control** — empty room vs. the same empty room later, no object in either. 0.5 is clean. Above 0.60 the bundle is flagged untrustworthy regardless of what [A] said.

Each of the first three also reports a **meta-only model on the same split** (RSSI/AGC/FFT gain, no channel data). If the CSI model does not clear that line, it is not using the channel.

**On the bundled `sample_data/csi_packets.csv`, it does not.** Measured:

```text
variant                              dims   sessCV  blockCV  holdout
meta-only (3 scalars, no CSI)           3      n/a    0.956    0.888
v3-style (raw gain + meta + seq)     4460      n/a    0.971    0.888
v4 default                           1032      n/a    0.976    0.906

Negative control — empty room vs. the same empty room, later:
  bal_acc=0.920  auc=0.946  over 1114 windows
```

That last line is the one that matters. Splitting the **baseline session against itself** — no object present in either half — scores **0.920**, *higher* than the real empty-vs-object hold-out of 0.906. Whatever the model has learned, an empty room also has it. Contributing causes: the two sessions were recorded back-to-back rather than interleaved, so slow drift tracks the label perfectly; object RSSI (−47.0) is *higher* than baseline (−48.4), which is the wrong sign for something blocking the path.

**To get a number that means "object detected"**, re-capture per the protocol in [`MAC_MINI.md`](MAC_MINI.md#steps-12--capture-interleaved): ~15 interleaved 2-minute blocks per class in one sitting, varying object position / object type / TX–RX separation across blocks, plus a couple of `baseline_nudge_*` blocks (empty room, boards moved ~5 cm) as a geometry control. Then tier [A] becomes available and the negative control should drop toward 0.5.


Output example:

```text
EMPTY   P(object)=0.12  raw=0.08  thr=0.83  seq=32090 rssi=-53
OBJECT  P(object)=0.91  raw=0.88  thr=0.83  seq=17201 rssi=-47
```

Waits for **30 packets** (~2 s at the measured ~13.6 pkt/s in-burst rate) before the first prediction, then updates every **15 packets** — or every packet with `--fast`. A gap longer than the window span resets the buffer rather than predicting across a stall.

---

## 4. View data in Postgres

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
psql postgresql:///csi
```

Inside `psql`:

```sql
-- list tables
\dt

-- all capture runs
SELECT id, method, label, recv_port, started_at, ended_at
FROM csi_sessions
ORDER BY started_at DESC;

-- packet counts per run
SELECT s.label, count(*) AS packets,
       min(c.host_ts) AS first_pkt, max(c.host_ts) AS last_pkt
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
GROUP BY s.label
ORDER BY max(c.host_ts) DESC;

-- latest packets (readable — do NOT SELECT * ; iq is huge)
SELECT seq, mac, rssi, channel, len, host_ts, iq[1:6] AS iq_head
FROM csi_samples
ORDER BY host_ts DESC
LIMIT 10;

-- one session only (paste session id from above)
SELECT count(*), min(rssi), max(rssi),
       avg(array_length(iq, 1))::int AS iq_len
FROM csi_samples
WHERE session_id = 'PASTE-UUID-HERE';

-- dimensions check (expect len=234, pairs=117)
SELECT len, array_length(iq, 1) AS iq_n,
       array_length(iq, 1) / 2 AS iq_pairs
FROM csi_samples
LIMIT 5;

\q
```

**Pager tip:** if `SELECT *` freezes on a wall of `iq`, press **`q`**. Prefer the `iq[1:6]` query above.

**More queries:** [`queries.sql`](queries.sql) is a snippet library covering inspection, capture health (sample rate, jitter, packet loss), amplitude/phase extraction from `iq`, CSV export, and data deletion. Open `psql` and paste blocks from it — do not run it with `psql -f`, since it ends with destructive statements.

One-shot from the shell (no interactive `psql`):

```bash
psql postgresql:///csi -c "SELECT label, count(*) FROM csi_sessions s JOIN csi_samples c ON c.session_id=s.id GROUP BY label;"
```

```bash
ls /dev/cu.usb*
./run_ingest.sh --probe
```

---

## 6. Expected packet shape (ESP32-C5 §4.3)

| Field | Typical |
|-------|---------|
| `mac` | `1a:00:00:00:00:00` (fixed sender) |
| `channel` | `11` |
| `len` | `234` |
| `iq` | 234 signed ints, **imag, real, imag, real, …** |
| I/Q pairs | **117** |

Amplitude / phase are **not** stored; compute offline from `iq` when needed.

---

## Files

| Path | Role |
|------|------|
| [`MAC_MINI.md`](MAC_MINI.md) | **Mac Mini runbook** (4.1 / 4.2 / 4.3 + Postgres) |
| [`setup_mac.sh`](setup_mac.sh) | One-time machine setup |
| [`run_ingest.sh`](run_ingest.sh) | Capture launcher (loads `.env`, uses `uv run --group csi`) |
| [`run_detect.sh`](run_detect.sh) | Train / calibrate / ablate / self-test / live detect launcher |
| [`probe_recv_port.py`](probe_recv_port.py) | Detect recv USB port |
| [`ingest_serial.py`](ingest_serial.py) | Serial/file → batch INSERT |
| [`csi_features.py`](csi_features.py) | v4 feature builder + `FeatureConfig` (shared by train and live) |
| [`train_object_detector.py`](train_object_detector.py) | Trainer: 3-tier evaluation, leakage baselines, bundle |
| [`ablate.py`](ablate.py) | Feature ablation + empty-vs-empty negative control |
| [`self_test.py`](self_test.py) | Asserts train/live feature parity; optional hardware check |
| [`detect_live.py`](detect_live.py) | Live serial → windowed prediction |
| [`calibrate_site.py`](calibrate_site.py) | Empty-room recording → site baseline + threshold |
| [`cross_condition_eval.py`](cross_condition_eval.py) | Train on one condition, test on an unseen one |
| [`probe_check.py`](probe_check.py) | Does the object change the channel, or only the power? |
| [`run_positive_control.sh`](run_positive_control.sh) | Hands-free alternating empty/object capture |
| [`POSITIVE_CONTROL.md`](POSITIVE_CONTROL.md) | Physical rig spec: distances, heights, clearances |
| [`schema.sql`](schema.sql) | Table definitions |
| [`.env.example`](.env.example) | `DATABASE_URL` template |

Override DB with `DATABASE_URL` in `.env` if needed (default `postgresql:///csi`).
