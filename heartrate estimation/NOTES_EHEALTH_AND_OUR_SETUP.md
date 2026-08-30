# eHealth CSI paper → our ESP32-C5 CSI setup

**Paper:** *eHealth CSI: A Wi-Fi CSI Dataset of Human Activities* (IEEE Access 2023)  
**PDF:** [`eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf`](eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf)  
**Related:** Pulse-Fi trains/evaluates on this dataset — see [`NOTES_PULSE_FI_AND_OUR_SETUP.md`](NOTES_PULSE_FI_AND_OUR_SETUP.md)

---

## What eHealth is (one paragraph)

A **public CSI dataset** (request access via their dashboard) for **human activity, presence, and vital signs** research. **118 participants**, **17 body positions/activities** (60 s each), plus **empty-room** baselines and **smartwatch** heart rate / respiration as ground truth. Collected in a **3 m × 4 m** room with a **5 GHz Wi‑Fi router**, laptop **pinging** the router every **136 ms**, and a **Raspberry Pi 4B + Nexmon** capturing CSI into **pcap** files. **234 subcarriers** per packet (80 MHz bandwidth). Includes anonymous **phenotype** metadata (age, gender, height, weight, BMI, health flags).

---

## Collection setup (Section IV)

### Geometry (Fig. 1)

| Element | Placement |
|---------|-----------|
| Room | **3 m × 4 m**, tables + bed (furniture stays for all runs) |
| **Participant** | Marked spot on floor (**X**) |
| **Wi‑Fi router** (TX/AP) | **1 m** from participant |
| **Laptop** (client) | **1 m** from participant, **opposite** router |
| **Raspberry Pi** (CSI probe) | **1 m** from participant, equidistant from router & laptop |

Person is **in the middle** of the link — same idea as Pulse-Fi / good sensing geometry.

### RF / CSI parameters

| Parameter | eHealth | **Our setup (§4.3)** |
|-----------|---------|----------------------|
| Band | **5 GHz** | **2.4 GHz** |
| Channel | **36** | **11** |
| Bandwidth | **80 MHz** | **HT40** (~40 MHz) |
| **Subcarriers in CSI** | **234** | **234** (`len` in our rows) |
| Trigger | Laptop **ping → router** every **136 ms** | ESP‑NOW from `csi_send` |
| CSI capture device | **RPi + Nexmon** (1 antenna) | **ESP32-C5 `csi_recv`** |
| Stored rate (effective) | **~1/0.136 ≈ 7.4 Hz** from ping interval | **~4–5 Hz** (USB serial) |
| Ground truth | **Samsung Galaxy Watch 4** (HR + respiration) | **None** on our snapshots |
| Empty baseline | **Empty room CSI before each participant** | Your `baseline_1hr` (no person) — similar idea |

**Important:** eHealth is **router / ping CSI** (closest to our **§4.1**), not ESP‑NOW §4.3. But **234 subcarriers** matches our vector length almost exactly — Pulse-Fi uses eHealth partly for that reason.

### Why 136 ms ping?

Paper states vital signs live in **0.2–3.5 Hz** (breathing + heartbeat). Ping interval sets sampling rate; **136 ms ≈ 7.4 Hz** must exceed **2× max frequency** (~7 Hz) to avoid aliasing of **3.5 Hz** content.  
→ Even the **reference dataset** is **not** 80 Hz; it’s **~7 Hz**. Our **~5 Hz** is in the same ballpark (still marginal for fine HR).

### Protocol (17 positions × 60 s)

Examples: sitting, standing, lying, walking, breath-hold cycles, etc. (Table 2 in paper).  
Each position: **60 seconds**, participant alone in room, instructions on screen + stopwatch.

**Empty room:** collected **before each participant** — used as channel baseline (presence paper compares filled vs empty with DTW).

### Participants

- **118** total (88 M / 30 F), ages **18–64**
- Phenotype + health questionnaire (anonymous in dataset)
- Ethics approval (Brazilian Ministry of Health)

---

## What’s in the dataset (downloads)

| Data type | Use |
|-----------|-----|
| **Raw CSI** (pcap) | Amplitude/phase per subcarrier, time series |
| **Smartwatch** | HR + respiration **labels** per position |
| **Phenotype** | Age, gender, height, weight, BMI, … |
| **Empty room** | Baseline channel without person |
| **Processed examples** | Dashboard shows CSI-estimated vs watch HR |

Access: request form on their homepage (paper §VI) — **not a direct public zip**; credentials after approval.

---

## How they use the data (examples in paper)

### Vital signs (dashboard)

- Per participant, per position: **CSI-derived HR** vs **watch HR** (two dots per position on graphs).
- Raw CSI in DB; dashboard shows **processed** traces.

### Presence detection (§VII) — methodology we can copy

1. **Preprocess:** Hampel filter → moving average → **amplitude** per subcarrier  
2. **234 amplitudes × time** for each 60 s clip  
3. **DTW distance** vs **empty-room reference** (per subcarrier) → features  
4. Classify empty vs occupied (SVM etc.) — **~99.9%** on balanced train, **~91%** on held-out 18 people  

**Lesson:** empty-room baseline is **first-class data** — you already have `baseline_1hr` / `object_1hr` in that spirit.

---

## eHealth vs our snapshots

| | eHealth | Our `baseline_1hr` / `object_1hr` |
|--|---------|-----------------------------------|
| Purpose | Multi-activity + vitals + phenotype | Pipeline test + static occlusion |
| Labels | 17 positions + watch BPM | 2 session labels |
| Subcarriers | **234** | **234** ✓ |
| Person | 118 people, varied poses | No person / static box |
| Empty baseline | Per-participant empty room | `baseline_1hr` |
| Rate | ~7 Hz | ~5 Hz |
| Ground truth | Watch | None |

Your export is **valid CSI** and structurally similar on **subcarrier count**, but **not** a substitute for eHealth (no positions, no watch, no cohort).

---

## eHealth vs Pulse-Fi (why both papers matter)

| | eHealth paper | Pulse-Fi paper |
|--|---------------|----------------|
| Role | **Dataset** description | **Method** (filter + LSTM) |
| ESP32 | No (RPi Nexmon) | Yes (ESP-HR-CSI @ 80 Hz) |
| eHealth data | **Defines** collection | **Uses** for LSTM eval (118 people) |
| Subcarriers | **234** | eHealth path uses same; ESP path **64** |

**Practical path for us:**
1. **Prototype Pulse-Fi pipeline on eHealth** (after access) — same 234-D shape as our `iq`.  
2. **Collect our own HR sessions** with watch + ESP, matching eHealth geometry (person between TX/RX).  
3. Compare whether **§4.3 ESP** can match **router ping CSI** for HR.

---

## Mapping our `iq` row → eHealth-style processing

```text
eHealth / Nexmon:  complex CSI per subcarrier (234 values)
Our Postgres/CSV:    iq[1..234] imag/real interleaved

Per subcarrier k:
  amplitude[k] = hypot(iq[2k-1], iq[2k])   # imag, real
  (optional) phase[k] = atan2(imag, real)   # eHealth presence example uses amplitude

Mask dead bins 58–60 (HT40 null) — eHealth 80 MHz has its own null/guard pattern; don't assume identical bin indices across bandwidths.

Time axis:
  eHealth: ~7.4 Hz from ping timestamps in pcap
  Ours:    use host_ts deltas (~5 Hz) — resample if needed before bandpass 0.8–2.17 Hz (Pulse-Fi)
```

---

## What we should do next (eHealth-informed)

### Short term (no eHealth download yet)

- [ ] Treat **`baseline_1hr`** like eHealth **empty room** (reference for presence/occlusion).  
- [ ] Treat **`object_1hr`** like **channel perturbation** (static scatterer).  
- [ ] Add **smartwatch CSV** alongside future CSI runs (mirror eHealth).  
- [ ] Document **geometry** (distance, heights) like eHealth Table/Fig. 1.

### Medium term

- [ ] **Request eHealth dataset access** — train Pulse-Fi-style pipeline on **234 subcarriers** before custom ESP HR data.  
- [ ] Implement **amplitude + Hampel + moving average** (their presence pipeline) on our CSV export as exercise.  
- [ ] **HR capture:** person between boards, 60 s × multiple poses, watch sync — same protocol spirit as eHealth Table 2.

### Long term (manager scale-out)

- eHealth model: **one probe** (RPi) + **router ping** — centralized CSI.  
- Manager model: **many ESPs push their own CSI** — need device IDs + network ingest (our Postgres schema extension).

---

## Key numbers to remember

| Item | Value |
|------|--------|
| Participants | 118 |
| Positions | 17 × **60 s** |
| Subcarriers | **234** |
| Ping interval | **136 ms** (~7.4 Hz) |
| Vital sign band (paper) | **0.2–3.5 Hz** |
| Watch | Galaxy Watch 4 |
| Room | 3 m × 4 m |
| Device distances | **1 m** from participant |

---

## One-liner for manager

> “eHealth is the standard **234-subcarrier** CSI dataset with **118 people**, **17 poses**, and **smartwatch ground truth** — Pulse-Fi benchmarks on it. Our ESP pipeline already stores **234-length CSI vectors**; we’re aligned on **data shape**, but we still need **watch labels**, **human poses**, and **higher/stable sample rate** for vital-sign work. We can prototype on **eHealth** while improving our ESP capture.”

---

## Files in this folder

| File | Role |
|------|------|
| [`eHealth_CSI_...pdf`](eHealth_CSI_A_Wi-Fi_CSI_Dataset_of_Human_Activities.pdf) | Dataset paper |
| [`Pulse-Fi_...pdf`](Pulse-Fi_A_Low-Cost_System_for_Accurate_Heart_Rate_Monitoring_Using_Wi-Fi_Channel_State_Information.pdf) | HR method using eHealth + ESP |
| [`NOTES_PULSE_FI_AND_OUR_SETUP.md`](NOTES_PULSE_FI_AND_OUR_SETUP.md) | Pulse-Fi ↔ our setup |
| [`../sample data /csi_packets.csv`](../sample%20data%20/csi_packets.csv) | Our exported snapshots |
| [`../csi_pipeline/`](../csi_pipeline/) | Ingest + Postgres |
