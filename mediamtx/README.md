# MediaMTX lab

Viewers talk to **MediaMTX on the Mac Mini**, not to the ESP.

```
XIAO CameraWebServerWiFi          Mini ffmpeg              MediaMTX              Viewer
http://10.128.93.25:81/stream ──► MJPEG→H.264 ──────────► :8554/cam_xiao ──► ffplay / VLC
```

**Why not Micro-RTSP alone?** Raw MJPEG RTSP from the board does not decode in ffplay 9 (`nan : 0.000`). H.264 via ffmpeg does.

| Host | Role | IP |
|------|------|-----|
| XIAO Sense | HTTP MJPEG `:81/stream` | `10.128.93.25` |
| Mac Mini | ffmpeg + MediaMTX | `10.128.93.23` |

Firmware: [`../firmware/CameraWebServerWiFi/`](../firmware/CameraWebServerWiFi/)  
Flash: `./scripts/flash_camera_webserver.sh` or arduino-cli (see firmware README).

---

## Every run (Mac Mini)

**Terminal 1:**
```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao' || true
XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
```
Wait for: `first frame OK`

**Terminal 2 (low delay):**
```bash
./scripts/watch_xiao_rtsp.sh
# or:
ffplay -rtsp_transport tcp -fflags nobuffer -flags low_delay -framedrop -sync ext \
  rtsp://127.0.0.1:8554/cam_xiao
```

Defaults: 12 fps, 1500k, x264 `ultrafast` + `zerolatency` (lower lag than quality preset).

**Colleague (LabPSK + VLC):** `rtsp://10.128.93.23:8554/cam_xiao` (force TCP).

---

## Files

| File | Purpose |
|------|---------|
| `mediamtx.yml` | Lab config (`cam_xiao` = publisher + runOnInit) |
| `mediamtx.runtime.yml` | Generated — gitignored |
| `../scripts/mediamtx_run.sh` | Starts MediaMTX + publish wrapper |
| `../scripts/publish_xiao.sh` | ffmpeg MJPEG→H.264 |
| `../scripts/watch_xiao_rtsp.sh` | Simple ffplay helper |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `first frame` never appears | Mini can’t reach ESP `:81`. Power/reset XIAO; `curl -I http://10.128.93.25:81/stream` |
| ffplay `404` / connection refused | MediaMTX not running or no publisher yet |
| `nan : 0.000` on ESP `:554` | Expected with Micro-RTSP + ffplay 9 — use this H.264 path instead |
