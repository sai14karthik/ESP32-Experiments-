# Mac Mini — CSI capture & PostgreSQL (methods 4.1, 4.2, 4.3)

End-to-end guide for the **Mac Mini** as the data host: flash ESP32-C5 boards, capture CSI over USB, store in local Postgres.

| Method | Name | AP needed? | Boards | What prints `CSI_DATA` |
|--------|------|------------|--------|-------------------------|
| **4.1** | Router CSI | **Yes** | 1× C5 | The one board (from router ping replies) |
| **4.2** | Between devices | **Yes** | 2× C5 | **Sense** board (peer traffic) |
| **4.3** | ESP-NOW pair | **No** | 2× C5 | **Recv** board (sender on power only) |

Firmware flashing is documented in [`CSI_METHODS.md`](../CSI_METHODS.md) and [`install.MD`](../install.MD). This doc focuses on **running captures on the Mini** and **Postgres ingest**.

---

## Architecture

```text
                    ┌─────────────────────────────────────────┐
  4.1  one C5 ─────►│  USB serial  →  run_ingest.sh  →  Postgres │
       + Wi-Fi AP   │         (Mac Mini, database: csi)       │
                    └─────────────────────────────────────────┘

  4.2  peer C5 ────►│  (power / optional USB)                 │
       sense C5 ───►│  USB serial  →  ingest  →  Postgres     │
       + same AP    └─────────────────────────────────────────┘

  4.3  send C5 ────►│  power only (no USB to Mini)              │
       recv C5  ───►│  USB serial  →  ingest  →  Postgres     │
       no AP        └─────────────────────────────────────────┘
```

**Rule:** Only the board that **prints `CSI_DATA`** needs USB on the Mini. The other board (4.2 peer, 4.3 sender) only needs power and radio range.

**Do not** run `idf.py monitor`, `screen`, or `plot_csi.sh` on the same port while `run_ingest.sh` is running.

---

## One-time setup on the Mac Mini

### 1. Clone repo & ESP-IDF (for flashing)

Follow [`install.MD`](../install.MD) — ESP-IDF **6.0.x**, flash helpers under `scripts/`.

```bash
cd ~/Desktop/camera_module   # or your clone path
source "$HOME/.espressif/tools/activate_idf_v6.0.2.sh"
```

### 2. Postgres + ingest pipeline

```bash
cd csi_pipeline
./setup_mac.sh
```

This installs **PostgreSQL 16** (Homebrew), creates database `csi`, applies [`schema.sql`](schema.sql), creates Python `.venv`, and writes [`.env`](.env).

Add Postgres to your shell (optional, add to `~/.zshrc`):

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
export DATABASE_URL='postgresql:///csi'
```

### 3. Verify

```bash
psql postgresql:///csi -c "SELECT 1;"
cd csi_pipeline && ./run_ingest.sh --from-file fixtures/sample_csi_lines.csv --method 4.3 --label setup_test
```

---

## Daily workflow (all methods)

```bash
cd ~/Desktop/camera_module/csi_pipeline

# 1. Which USB port is the CSI board?
./run_ingest.sh --probe

# 2. Capture (use --method matching your firmware)
./run_ingest.sh --method 4.3 --channel 11 --label my_run_name

# 3. Ctrl+C to stop

# 4. Check data
psql postgresql:///csi -c "
  SELECT s.label, count(*) FROM csi_sessions s
  JOIN csi_samples c ON c.session_id = s.id
  GROUP BY s.label ORDER BY max(c.host_ts) DESC LIMIT 5;"
```

Use a **descriptive `--label`** every time (`baseline_empty`, `object_box`, `lab_desk`, …). Each run creates **one** row in `csi_sessions` and many rows in `csi_samples`.

---

## Method 4.3 — ESP-NOW (recommended for controlled lab data)

**No Wi‑Fi AP.** Sender and receiver on **channel 11**, HT40.

### Flash (on any Mac with IDF; both boards plugged in)

```bash
cd ~/Desktop/camera_module
./scripts/flash_csi_pair.sh
# or: ./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101
#      first port = send, second = recv
```

### Hardware on the Mini

| Board | Connection |
|-------|------------|
| **csi_recv** | USB → Mac Mini |
| **csi_send** | **Power only** (charger / USB power, not data) |

Place boards **~1.5–2 m** apart; subject or object **between** TX and RX for sensing experiments. Target RSSI **−35 to −55 dBm** (check after capture).

### Ingest on the Mini

```bash
cd csi_pipeline
./run_ingest.sh --probe
./run_ingest.sh --method 4.3 --channel 11 --label baseline_empty
```

| Expect | Value |
|--------|--------|
| `mac` in packets | `1a:00:00:00:00:00` |
| `channel` | `11` |
| `len` / `iq` | `234` ints → 117 I/Q pairs |
| Stored rate | ~4–15 pkt/s (USB 115200 limit; sender targets 100 Hz) |

---

## Method 4.1 — Router CSI

**One** C5 joins your lab AP; CSI comes from **router** traffic (ping replies). Filter MAC = AP BSSID.

### Flash

```bash
cd ~/Desktop/camera_module
./scripts/set_csi_wifi.sh 'YourSSID' 'YourPassword' /dev/cu.usbmodem101
```

Mini must reach the **same network** as the AP (Ethernet or Wi‑Fi). Client isolation on the AP can block pings — disable for lab if needed.

### Hardware on the Mini

| Board | Connection |
|-------|------------|
| **csi_recv_router** | USB → Mac Mini |

### Ingest

```bash
cd csi_pipeline
./run_ingest.sh --port /dev/cu.usbmodem101 --method 4.1 --label router_run1
```

**`--channel`:** omit or set after first packets — channel comes from the AP (read from data):

```sql
SELECT DISTINCT channel FROM csi_samples
WHERE session_id = (SELECT id FROM csi_sessions ORDER BY started_at DESC LIMIT 1);
```

| Expect | Value |
|--------|--------|
| `mac` | Your **router / AP BSSID** (not `1a:00:…`) |
| AP | Required and must match flash credentials |

**Note:** `./monitor_csi.sh` is only for 4.1 debugging — do not use it at the same time as ingest.

---

## Method 4.2 — Between two devices (same AP)

**Two** C5s join the **same** AP. **Sense** board measures CSI from **peer** MAC `1a:00:00:00:00:0a`.

### Flash

```bash
cd ~/Desktop/camera_module
./scripts/flash_csi_between.sh 'YourSSID' 'YourPassword' /dev/cu.usbmodem101 /dev/cu.usbmodem2101
# first port = PEER, second = SENSE (ingest this one)
```

### Hardware on the Mini

| Board | Connection |
|-------|------------|
| **Sense** (CSI output) | USB → Mac Mini |
| **Peer** (traffic generator) | Power or second USB (no ingest on peer port) |

### Ingest (sense port only)

```bash
cd csi_pipeline
./run_ingest.sh --probe          # pick the port with CSI_DATA
./run_ingest.sh --port /dev/cu.usbmodem2101 --method 4.2 --label between_run1
```

| Expect | Value |
|--------|--------|
| `mac` | `1a:00:00:00:00:0a` (peer) |
| AP | Required; both boards on same SSID |

---

## Compare methods (quick)

| | 4.1 | 4.2 | 4.3 |
|---|-----|-----|-----|
| AP | Yes | Yes | No |
| Boards on Mini USB | 1 | 1 (sense) | 1 (recv) |
| Ingest `--method` | `4.1` | `4.2` | `4.3` |
| Typical `mac` | Router BSSID | `…0a` | `…00` |
| Best for | Real deployment / router path | Two STAs on LAN | Controlled lab / ML datasets |

Store the method on every session via `--method` so you can filter in SQL:

```sql
SELECT method, label, count(*) FROM csi_sessions s
JOIN csi_samples c ON c.session_id = s.id
GROUP BY method, label;
```

---

## View & export data

### psql

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
psql postgresql:///csi
```

```sql
\pset pager off
SELECT id, method, label, started_at, ended_at FROM csi_sessions ORDER BY started_at DESC;
SELECT seq, mac, rssi, channel, len, iq[1:6] FROM csi_samples ORDER BY host_ts DESC LIMIT 10;
```

More queries: [`queries.sql`](queries.sql) (paste blocks; do not `psql -f` whole file — contains deletes).

### Export CSV

```bash
mkdir -p exports
psql postgresql:///csi
```

```sql
\copy (SELECT * FROM csi_sessions ORDER BY started_at) TO 'exports/sessions.csv' WITH CSV HEADER
\copy (
  SELECT s.label, s.method, c.*
  FROM csi_samples c
  JOIN csi_sessions s ON s.id = c.session_id
  WHERE s.label = 'baseline_empty'
  ORDER BY c.host_ts
) TO 'exports/baseline_empty.csv' WITH CSV HEADER
```

Copy `exports/` to another machine via AirDrop, `scp`, or shared drive.

---

## Packet shape (ESP32-C5, all methods)

| Field | Typical |
|-------|---------|
| `len` | `234` |
| `iq` | 234 ints, **imag, real, …** → 117 complex bins |
| Dead bins | **58, 59, 60** always zero — mask before ML |
| Amplitude | \(\sqrt{I^2+Q^2}\) per bin, computed offline |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No /dev/cu.usb*` | Plug recv/sense board; try another cable/port |
| `Port is busy` | Quit monitor/screen/plotter on that port |
| `./run_ingest.sh --probe` shows CSI_DATA=0 on all ports | Wrong firmware (flash recv/sense/router image); or 4.3 sender not powered |
| Two ports, unsure which is recv | Use port with **CSI_DATA > 0** |
| `iq_ok` / `len` mismatch | Rare parse error; check baud **115200** |
| RSSI ~−10, `agc_gain=0` | Boards too close — move to ~2 m |
| Very few packets vs long run | Normal: USB ~5–15 pkt/s, not 100 Hz |
| 4.1 no `CSI_DATA` | Wi‑Fi credentials, AP isolation, no `got ip` on board |
| Postgres connection failed | `brew services start postgresql@16`; check `.env` |

---

## File reference

| Path | Purpose |
|------|---------|
| [`setup_mac.sh`](setup_mac.sh) | One-time Mini setup |
| [`run_ingest.sh`](run_ingest.sh) | Start capture |
| [`probe_recv_port.py`](probe_recv_port.py) | Find CSI USB port |
| [`ingest_serial.py`](ingest_serial.py) | Serial → Postgres |
| [`schema.sql`](schema.sql) | DB tables |
| [`queries.sql`](queries.sql) | SQL snippets |
| [`../CSI_METHODS.md`](../CSI_METHODS.md) | Flash & monitor details |
| [`../install.MD`](../install.MD) | IDF & first-time toolchain |

---

## Suggested lab placement (4.3 sensing)

- **Recv** USB → Mac Mini  
- **Send** on power, **~2 m** from recv, same height  
- **Fixed tape** — do not move boards between labeled runs  
- **Empty** vs **object** runs: change only the scene and `--label`, not geometry  

Example sequence:

```bash
./run_ingest.sh --method 4.3 --channel 11 --label baseline_empty
# Ctrl+C after desired duration
./run_ingest.sh --method 4.3 --channel 11 --label static_object
```
