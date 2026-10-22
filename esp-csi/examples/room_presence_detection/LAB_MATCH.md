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

## Detection formula — matches limengdu again

Per-link rule is theirs: `value × sensitivity > threshold`.

- Link 1 (master): master calibration × its sliders
- Links 2 and 3: each slave's own calibration and its own yes/no (ESP-NOW report)
- Slave wander/jitter math is the same function as upstream

Two stability extras, not a different formula:

- Always need **≥2** links (upstream can fall back to 1 if a link drops)
- Debounce 1 s to enter, 2.5 s to leave

The temporary `0.30` / `0.12` threshold scales are removed. They were why motion would not trip unless the slider was pushed very high.

Slaves ignore ESP-NOW from the TX MAC `1a:00:…`. The TX packet counter's low byte is `0x10`–`0x1F`, which upstream treats as calibration commands and wipes the presence baseline.

## Flash map (this lab)

| Board | MAC | Firmware |
|-------|-----|----------|
| TX | `…:a8:e8` | `send_TX` |
| RX1 | `…:a5:20` | `recv_master_RX1` |
| RX2 | `…:db:b0` | `recv_slave` **node_id=1** |
| RX3 | `…:59:28` | `recv_slave` **node_id=2** |

Edit `CONFIG_SLAVE_NODE_ID` in source before each slave flash (`-D` does not reach the C compiler).

## Collect onto the Mini

All four boards join lab Wi-Fi `LabHealthSecurePSK` and follow that access point’s channel. The master pushes the three-link status to the Mini at `10.128.93.13` port `9056`. The Mini stays on Ethernet.

The phone page on lab Wi-Fi is `http://10.128.93.25`.

On the Mini, the same command stores board status and a camera ground-truth label (`cv_occupied`: 0 empty, 1 occupied) from YOLO26-pose on `http://10.128.93.15:81/stream`:

```bash
./tools/collect_to_mini.sh --session pipeline_test
```

Rows land in Postgres database `csi`, tables `room_presence_sessions` and `room_presence_samples`. A line like `rows=20 ... cv=occupied` means the camera label is being saved. Flash all four boards before powering the set; a mix of old channel-11 firmware and this build will not see each other.
