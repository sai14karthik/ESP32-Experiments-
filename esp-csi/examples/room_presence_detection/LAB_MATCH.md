# Follow limengdu / Hackster / Reddit (deep audit)

Sources (same commit we imported):
- https://www.hackster.io/limengdu0117/esp-csi-diy-wifi-human-presence-detection-f80508
- https://github.com/limengdu/XIAO_esp-csi → `examples/room_presence_detection` @ `6b40b015`

Re-audited against upstream `6b40b015` (Oct 2026).

## Architecture — matches Reddit

```
TX (soft MAC 1a:00…) ──CSI──► RX1 master (SoftAP RoomSensor + fuse + web)
                     ──CSI──► RX2 slave node_id=1 ──ESP-NOW report──► RX1
                     ──CSI──► RX3 slave node_id=2 ──ESP-NOW report──► RX1
Phone ──Wi‑Fi RoomSensor──► http://192.168.4.1
```

| Piece | vs limengdu |
|-------|-------------|
| Soft MAC `1a:00:00:00:00:00`, ch **11**, 100 Hz | Same |
| AP `RoomSensor` / `12345678` | Same |
| `slave_report_t` / `link_status_t` layouts | Same |
| ESP-NOW cmds `0x10` cal start / `0x11` stop / `0x13` sens | Same |
| `trimmean` / `median` | Same |
| Slave wander/jitter detection math (local) | Same |
| 30 s empty cal + NVS thresholds/sensitivity | Same |
| Link map: master=0, slave1→Link2, slave2→Link3 | Same |

## Port fixes (required — all-C6 + IDF 6 + esp-radar 0.3.4)

Upstream targets mixed XIAO + **HT40**. Stock limengdu on our four C6s stayed Offline / cal no-op until:

| Change | Why |
|--------|-----|
| Soft MAC **before** `wifi_start` on TX | IDF6/C6 ignores post-start soft MAC |
| RF **HT20** + `SECOND_CHAN_NONE` | All-C6 SoftAP+CSI |
| `RADAR_LTF_TYPE_HTLTF` | LLTF empty on HT ESP-NOW |
| `esp_radar_new_peer` + `wifi_radar_cb_ex` | 0.3.4 peer path |
| `train_start/stop_ex` on peer handle | Legacy `train_*` needs `default_peer` (never set) → cal left defaults |
| WS/`/api/status` 1024 + always poll | Truncated UI |
| IDF6 `WIFI_IF_STA` / BW aliases | Compile |

## Intentional fusion drift (lab tuning — not 1:1 with upstream fuse)

Upstream fuse:
- Link 0: master threshold × sens
- Links 1–2: **trust slave boolean flags**
- Vote: `min = (active>=2)?2:1` (can fall back to **1**)
- No debounce

Ours (after empty false-Motion / blink / walk-miss on C6):
- **All 3 links** scored from raw wander/jitter with **master** cal × sens × scale  
  (`WANDER_TH_SCALE=0.30`, `JITTER_TH_SCALE=0.12`)
- Slave flags still computed/sent but **not used for fuse**
- Always need **≥2** links (never fall back to 1)
- Debounce enter 1 s / exit 2.5 s

Same *idea* (multi-link vote, empty cal, sensitivity). Different *fuse details* tuned so empty stays Empty and walks still register on this hardware.

## Flash map (this lab)

| Board | MAC | Firmware |
|-------|-----|----------|
| TX | `…:a8:e8` | `send_TX` |
| RX1 | `…:a5:20` | `recv_master_RX1` |
| RX2 | `…:db:b0` | `recv_slave` **node_id=1** |
| RX3 | `…:59:28` | `recv_slave` **node_id=2** |

Edit `CONFIG_SLAVE_NODE_ID` in source before each slave flash (`-D` does not reach the C compiler).

## Not this path

`csi_presence_c5` Mini TCP `:9055` / LabPSK — separate. This tree is SoftAP phone UI only.
