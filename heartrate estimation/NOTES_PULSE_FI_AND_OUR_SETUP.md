# Pulse-Fi paper → our ESP32-C5 CSI setup

**Paper:** *Pulse-Fi: A Low-Cost System for Accurate Heart Rate Monitoring Using Wi-Fi Channel State Information* (DCOSS-IoT 2025)  
**PDF:** [`Pulse-Fi_A_Low-Cost_System_for_Accurate_Heart_Rate_Monitoring_Using_Wi-Fi_Channel_State_Information.pdf`](Pulse-Fi_A_Low-Cost_System_for_Accurate_Heart_Rate_Monitoring_Using_Wi-Fi_Channel_State_Information.pdf)  
**eHealth dataset (used by Pulse-Fi):** [`NOTES_EHEALTH_AND_OUR_SETUP.md`](NOTES_EHEALTH_AND_OUR_SETUP.md)

---

## What Pulse-Fi does (one paragraph)

Low-cost **Wi-Fi CSI → signal processing → small LSTM** to estimate **heart rate (BPM)** without contact. Uses **amplitude only** (no multi-antenna phase difference). Evaluated on **ESP-HR-CSI** (their own ESP32 captures) and **EHealth** (118 people, Raspberry Pi + Nexmon, 234 subcarriers). Best windows **≥5 s**, optimal **~30 s**; reports **MAE ~0.46 BPM** on ESP data at 1–3 m.

---

## Pulse-Fi pipeline (Section IV–V)

| Stage | What they do |
|-------|----------------|
| 1. **Amplitude** | \(\|H_k\| = \sqrt{I^2 + Q^2}\) per subcarrier; **no phase** (single antenna) |
| 2. **Stationary noise removal** | Remove DC component |
| 3. **Bandpass** | 3rd-order Butterworth **0.8–2.17 Hz** (48–130 BPM) |
| 4. **Pulse shaping** | Savitzky–Golay (window 15, poly order 3) |
| 5. **Segmentation** | Overlapping windows of **N packets** (e.g. 100 packets); normalize |
| 6. **LSTM** | Sequence in → BPM out; Adam, MSE; train/val/test **64/16/20%** |

**Key idea:** heartbeat is a **slow periodic change in CSI amplitude** (~0.8–2 Hz). Need **enough time** in each window (they say &lt;1 s is too short; **5–30 s** works).

---

## Their ESP32 collection (ESP-HR-CSI) — closest to us

| Parameter | Pulse-Fi (ESP-HR-CSI) | **Our setup (§4.3 + pipeline)** |
|-----------|------------------------|----------------------------------|
| Hardware | 2× ESP32, **1 TX + 1 RX**, single antenna each | 2× **ESP32-C5**, `csi_send` + `csi_recv` |
| Link | Dedicated TX/RX (commodity) | ESP-NOW, ch **11**, HT40 |
| **Sampling (stored)** | **80 Hz** | **~4–5 Hz** (USB 115200 bottleneck) |
| Bandwidth / bins | **20 MHz**, **64 subcarriers** | **HT40**, **117 bins** (`len=234` ints) |
| Geometry | Person **between** TX and RX | Baseline/object tests; aim **~2 m**, person on link |
| Distances | **1, 2, 3 m** × **5 min** each | You used ~2 m; good match to their range |
| Ground truth | **Pulse oximeter** on finger | **None yet** — required for HR training/eval |
| Participants | 7 people, library room | 2 static conditions (empty vs object) — not HR |
| Features used | Amplitude time series | Full `iq` in Postgres/CSV (can derive amplitude) |

---

## What already aligns

- **Same class of hardware:** cheap ESP32-family, **one sender + one receiver**, amplitude-based HR is explicitly what they use.
- **Similar geometry intent:** TX–RX separation, subject in the middle (their Fig. 4).
- **Subcarrier count in the same ballpark:** we have **more** bins (117 vs 64) after dropping dead bins (~114 usable).
- **Data format:** we store `iq` (imag/real) → can compute amplitude exactly as in the paper.
- **EHealth in same folder:** same paper uses [`eHealth_CSI_...pdf`](eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf) — **234 subcarriers**, very close to our `len=234`.

---

## Critical gaps for heart-rate (must fix)

### 1. Sampling rate (~5 Hz vs 80 Hz)

- Pulse-Fi assumes **many packets per second** for bandpass + LSTM windows.
- At **5 Hz**, Nyquist for 2.17 Hz is **barely** OK in theory, but:
  - **30 s window** = only **~150 packets** for us vs **~2400** at 80 Hz.
  - Heartbeat-induced amplitude wobble is **subtle**; undersampling loses phase of the pulse train.
- **Action:** raise effective rate before serious HR work:
  - Higher UART baud and/or binary CSI export on `csi_recv`
  - Or Wi-Fi push from device (manager’s scale-out direction)
  - Or lower `CONFIG_SEND_FREQUENCY` to match USB (~10 Hz) for steadier stream

### 2. No ground truth

- Pulse-Fi **requires** pulse oximeter (ESP-HR-CSI) or smartwatch (EHealth) labels.
- **Action:** add **PPG / pulse ox / smartwatch** logging synced by time for any HR capture session.

### 3. Wrong experiment type so far

- Your **`baseline_1hr` / `object_1hr`** = static empty vs static box → good for **occlusion / link test**, not heartbeat.
- **Action:** new sessions, e.g. `hr_rest_1m`, `hr_after_walk`, person **still** between boards, **5+ min**, with oximeter CSV.

### 4. Processing not implemented

- We store raw packets only. Pulse-Fi needs **per-subcarrier amplitude → DC remove → bandpass 0.8–2.17 Hz → SG smooth → windows → LSTM**.
- **Action:** offline Python notebook/script on exported CSV first; model later.

### 5. Dead bins

- Bins **58–60** (and sometimes neighbors) are always zero — **mask before** amplitude features (see `csi_pipeline/queries.sql` dead-bin query).

### 6. AGC / normalization

- Paper removes DC; we should also **normalize per packet** (divide by mean amplitude) because `agc_gain` / `fft_gain` vary.

---

## Suggested HR experiment (mirror ESP-HR-CSI)

1. **Placement:** TX and RX **1–2 m** apart, **same height**, person **seated between**, RSSI **−35 to −55 dBm**.
2. **Duration:** **5 min** per condition (they used 5 min × 3 distances).
3. **Ground truth:** finger pulse oximeter → log **timestamp + BPM** (1 Hz is enough).
4. **CSI capture:** `./run_ingest.sh --method 4.3 --channel 11 --label hr_p1_2m_rest`
5. **Improve rate** if possible before long captures (target **≥20 Hz** stored minimum for HR; **80 Hz** ideal).
6. **Align:** match `host_ts` (CSI) to oximeter time (same clock on Mini).

---

## Minimal processing recipe (from paper, for our `iq`)

```text
For each packet:
  amp[k] = hypot(iq[2k-1], iq[2k])   for k in 1..117, skip dead bins {58,59,60}
  optionally: amp[k] /= mean(amp)

Build time series per subcarrier (or mean across subcarriers):
  remove DC → bandpass 0.8–2.17 Hz → Savitzky-Golay (15, 3)

Windows: overlapping packet windows (e.g. 100 packets at your true fs)
  → LSTM → BPM

Train with oximeter BPM as label; report MAE / MAPE like Table I.
```

---

## What we can do **now** with existing snapshots

| Goal | Possible? |
|------|-----------|
| Train Pulse-Fi LSTM for HR | **No** — no BPM labels, ~5 Hz, static scenes |
| Validate CSI quality / empty vs object | **Yes** — done |
| Prototype **filtering only** on amplitude (see if ~1 Hz structure appears) | **Weak** — need person + higher fs |
| Train on **EHealth** dataset (public) | **Yes** — separate path; 234 subcarriers like ours; good for pipeline/LSTM code before our ESP data |

---

## Manager talking points

- **Aligned:** commodity ESP CSI, TX/RX link, amplitude-based HR, Postgres snapshot pipeline.
- **Gap:** we need **higher sample rate + ground truth + seated-human captures** to replicate Pulse-Fi.
- **Scale path:** today USB → Postgres; later each node pushes CSI (their LSTM is small enough for ESP32 edge inference per paper).

---

## Related file in repo

- Exported snapshots: [`../sample data /csi_packets.csv`](../sample%20data%20/csi_packets.csv)
- CSI pipeline: [`../csi_pipeline/README.md`](../csi_pipeline/README.md)
- EHealth paper + notes: [`eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf`](eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf) · [`NOTES_EHEALTH_AND_OUR_SETUP.md`](NOTES_EHEALTH_AND_OUR_SETUP.md)
