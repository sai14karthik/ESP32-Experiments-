# CSI Pipeline — Capture to PostgreSQL

ESP32-C5 CSI (§4.3 first) over USB → parse `CSI_DATA` → store in local PostgreSQL.  
Works the same on **this Mac** and a **Mac Mini**.

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

This installs/starts **PostgreSQL 16** (Homebrew), creates DB `csi`, applies [`schema.sql`](schema.sql), creates `.venv`, and writes `.env`.

Requires [Homebrew](https://brew.sh). Apple Silicon path: `/opt/homebrew/...`.

---

## 2. Hardware (§4.3)

1. Flash `csi_send` + `csi_recv` (see [`CSI_METHODS.md`](../CSI_METHODS.md)).
2. Plug the **receiver** USB into the Mac that runs ingest.
3. Keep the **sender** powered nearby (no Wi‑Fi AP needed).
4. Quit anything else on that serial port (`idf.py monitor`, `screen`, `plot_csi.sh`).

```bash
ls /dev/cu.usb*
./run_ingest.sh --probe          # which port prints CSI_DATA
```

---

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

One-shot from the shell (no interactive `psql`):

```bash
psql postgresql:///csi -c "SELECT label, count(*) FROM csi_sessions s JOIN csi_samples c ON c.session_id=s.id GROUP BY label;"
```

---

## 5. Mac Mini

Same repo folder and commands:

```bash
cd csi_pipeline
./setup_mac.sh
# plug csi_recv into the Mini
./run_ingest.sh --method 4.3 --channel 11 --label mini_desk
```

Sender only needs power nearby; it does not USB into the Mini.

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
| [`setup_mac.sh`](setup_mac.sh) | One-time machine setup |
| [`run_ingest.sh`](run_ingest.sh) | Capture launcher (loads `.env`, uses `.venv`) |
| [`probe_recv_port.py`](probe_recv_port.py) | Detect recv USB port |
| [`ingest_serial.py`](ingest_serial.py) | Serial/file → batch INSERT |
| [`schema.sql`](schema.sql) | Table definitions |
| [`.env.example`](.env.example) | `DATABASE_URL` template |

Override DB with `DATABASE_URL` in `.env` if needed (default `postgresql:///csi`).
