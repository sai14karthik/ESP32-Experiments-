# Follow limengdu / Hackster / Reddit (procedure + lab deltas)

Sources (same commit we imported):
- https://www.hackster.io/limengdu0117/esp-csi-diy-wifi-human-presence-detection-f80508
- https://github.com/limengdu/XIAO_esp-csi → `examples/room_presence_detection` @ `6b40b015`

## Their procedure (unchanged)

1. Flash **4 boards**: `send_TX` + `recv_master_RX1` + `recv_slave`×2 (`node_id` 1 and 2)
2. Place: TX corner, RX1 diagonal, RX2/RX3 other walls
3. Power all (wall USB) — Mac not required after flash
4. Phone Wi‑Fi **`RoomSensor`** / **`12345678`** → **`http://192.168.4.1`**
5. Empty room → **Start Calibration (30s)** → walk test

## Detection logic vs upstream — identical

Verified by function diff against limengdu `6b40b015`:

| Piece | Status |
|-------|--------|
| Soft TX MAC `1a:00:00:00:00:00`, ch **11**, 100 Hz (`csi_recv_interval=10`) | Same |
| AP `RoomSensor` / `12345678` | Same |
| `trimmean` / `median` / `recalculate_link_status` / `fuse_detection_results` | Same |
| Vote: `min_detection = (active_count >= 2) ? 2 : 1` | Same |
| 30 s calibration, NVS thresholds/sensitivity, slave ESP-NOW reports | Same |
| Slave `node_id` 1→Link2, 2→Link3 | Same |

We did **not** change presence/motion math or multi-link fusion.

## Lab deltas (all-C6 + IDF 6 + esp-radar 0.3.4)

Upstream README targets mixed XIAO (C3 TX, S3 master, C5/C6 slaves) with **HT40**. Our four boards are **ESP32-C6** on **ESP-IDF 6.0.2** + **esp-radar 0.3.4**. Stock limengdu firmware stayed Offline until these port fixes:

| Change | Why |
|--------|-----|
| Soft MAC **before** `wifi_start` on TX | After `wifi_start`, IDF6/C6 ignores soft MAC; RX filter never matches |
| RF **HT20** + `WIFI_SECOND_CHAN_NONE` (TX / SoftAP / slaves) | All-C6 SoftAP+CSI; HT40 left empty LLTF / no radar path |
| `ltf_type = RADAR_LTF_TYPE_HTLTF` | Default LLTF has no data on HT ESP-NOW frames (silent drop) |
| `esp_radar_new_peer(TX_MAC)` + `wifi_radar_cb_ex` | 0.3.4: legacy `wifi_radar_cb` only for `default_peer`; peer path needs `_ex` |
| Master: STA netif + APSTA HT20 bandwidths; re-arm CSI after `esp_now_init` | C6 APSTA bring-up |
| WS / `/api/status` buffer 1024 + always poll | Truncated JSON left UI on Initializing |
| IDF6: `WIFI_IF_STA`, `WIFI_BW20` aliases | Compile fixes |

## Flash map (this lab)

| Board | MAC | Firmware |
|-------|-----|----------|
| TX | `…:a8:e8` | `send_TX` |
| RX1 | `…:a5:20` | `recv_master_RX1` |
| RX2 | `…:db:b0` | `recv_slave` **node_id=1** |
| RX3 | `…:59:28` | `recv_slave` **node_id=2** |

Slave `node_id` is compile-time + NVS. After flashing RX2/RX3, erase NVS if an old id sticks (`idf.py erase-flash` then flash).

## Not this path

`csi_presence_c5` Mini TCP `:9055` / Postgres stays separate — this tree is the limengdu SoftAP phone UI only.
