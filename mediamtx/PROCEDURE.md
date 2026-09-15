# MediaMTX lab — canonical procedure (locked)

Do not invent flags. Follow this + MediaMTX docs.

## Why this shape

| Fact | Source |
|------|--------|
| ESP32-S3 has **no HW H.264** — outputs **MJPEG** | [Espressif FAQ](https://docs.espressif.com/projects/esp-faq/en/latest/application-solution/camera-application.html) |
| MediaMTX WebRTC/HLS need **H.264** | MediaMTX (MJPEG cannot feed HLS/WebRTC) |
| Host **ffmpeg** re-encodes, then publishes RTSP into MediaMTX | [Generic webcams](https://mediamtx.org/docs/publish/generic-webcams) |
| `runOnInit` + `runOnInitRestart: yes` | [Hooks](https://mediamtx.org/docs/features/hooks) |
| `writeQueueSize: 1024` fixes “reader is too slow, discarding N frames” | [Decrease packet loss](https://mediamtx.org/docs/features/decrease-packet-loss) |
| `hlsAlwaysRemux: no` (default) — don’t burn HLS when watching WebRTC | [Config reference](https://mediamtx.org/docs/references/configuration-file) |
| `hlsVariant: lowLatency` needs `hlsSegmentCount: 7` | MediaMTX validation |
| **WebRTC** for live; **HLS** is multi-second by design | MediaMTX protocol roles |

## Pipeline

```
XIAO CameraRTSPWiFi  →  rtsp://ESP:554/mjpeg/1  (MJPEG)
        ↓
Mac Mini ffmpeg      →  H.264  (ultrafast + zerolatency + fps=10)
        ↓
MediaMTX cam_xiao    →  WebRTC :8889  /  HLS :8888  /  RTSP :8554
```

## One-time

```bash
brew install mediamtx ffmpeg
brew services stop mediamtx   # avoid port fights
```

Flash board: `firmware/CameraRTSPWiFi` → serial shows `rtsp://10.128.93.25:554/mjpeg/1`.

## Every run (Mac Mini)

```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao' || true
cd ~/Desktop/ESP32-Experiments-
git pull
XIAO_RTSP_URL=rtsp://10.128.93.25:554/mjpeg/1 ./scripts/mediamtx_run.sh
```

Healthy log (after ~30s):

- `stream is available and online, 1 track (H264)`
- `is publishing to path 'cam_xiao'`
- **No** repeating `discarding 2xx frames`

## Watch

| Goal | URL |
|------|-----|
| **Live** (use this) | http://10.128.93.23:8889/cam_xiao/ |
| Backup | http://10.128.93.23:8888/cam_xiao/ |
| VLC | `rtsp://10.128.93.23:8554/cam_xiao` |

## Official ffmpeg shape we follow

MediaMTX webcam example:

`ffmpeg … -c:v libx264 -pix_fmt yuv420p -preset ultrafast -b:v 600k -f rtsp rtsp://localhost:$RTSP_PORT/$MTX_PATH`

Our ESP adaptation (in `scripts/publish_xiao.sh`):

- input: `-rtsp_transport tcp -i rtsp://ESP:554/mjpeg/1`
- filter: `-vf fps=10,format=yuv420p` (CFR — prevents encode flood)
- encode: `-preset ultrafast -tune zerolatency -b:v 600k -bf 0`
- out: `-f rtsp -rtsp_transport tcp` into MediaMTX

## Do not

- Watch HLS and expect &lt;1s live feel
- Set `writeQueueSize: 32` (causes mass discard)
- Leave `hlsAlwaysRemux: yes` while tuning WebRTC
- Publish uncapped fps (floods MediaMTX)
- Expect Mini `git pull` to get laptop edits that were never pushed
