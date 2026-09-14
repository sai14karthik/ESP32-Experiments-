# ESP32 / XIAO → MediaMTX (correct continuous path)

Research summary for this lab (Sep 2026). Sources are MediaMTX docs + maintainer answers.

## What MediaMTX officially says

| Question | Answer | Source |
|----------|--------|--------|
| Can MediaMTX ingest ESP **MJPEG over HTTP** natively? | **No.** Use **FFmpeg in `runOnInit`**. | [Discussion #3575](https://github.com/bluenviron/mediamtx/discussions/3575) (aler9) |
| How do viewers get all protocols? | Publish once into MediaMTX → read RTSP/HLS/WebRTC/RTMP | [Publish with FFmpeg](https://mediamtx.org/docs/publish/ffmpeg), [Hooks](https://mediamtx.org/docs/usage/hooks) |
| Do browsers work with MJPEG tracks? | **No.** HLS/WebRTC need **H.264** (etc.), not MJPEG | [HLS codecs](https://mediamtx.org/docs/read/hls), [WebRTC codecs](https://mediamtx.org/docs/read/webrtc) |
| How to stay continuous? | `runOnInitRestart: yes` relaunches FFmpeg when it exits | [Hooks](https://mediamtx.org/docs/usage/hooks) |
| When can you skip FFmpeg? | Only if the camera already outputs **H.264 RTSP** | [RTSP cameras](https://mediamtx.org/docs/publish/rtsp-cameras-and-servers) |

Maintainer example (MJPEG copy — **not enough for browsers**):

```yaml
paths:
  esp32:
    runOnInit: ffmpeg -i http://a.b.c.d/stream -c copy -f rtsp rtsp://localhost:8554/esp32
```

Lab correction: re-encode to **H.264 baseline** so HLS + WebRTC work.

## Architecture (this repo)

```
XIAO CameraWebServerWiFi
  http://10.128.93.25/capture   (polled)  or  :81/stream
        │
        ▼
  ffmpeg on Mini  (MJPEG → H.264 baseline)
        │
        ▼
  MediaMTX path cam_xiao
        │
        ├── RTSP   :8554/cam_xiao
        ├── HLS    :8888/cam_xiao/
        ├── WebRTC :8889/cam_xiao/
        └── RTMP   :1935/cam_xiao
```

Why **capture polls** by default: long-lived ESP `:81/stream` TCP often stalls (~1 min) on lab Wi‑Fi. Short GETs to `/capture` + MediaMTX restart recover cleanly. Expect brief blips — not CCTV-grade forever without a real H.264 camera.

## Board

Flash: `firmware/CameraWebServerWiFi`  
Serial should show: `http://10.128.93.25`

## Mini (one terminal)

Copy these files into `ESP32-Experiments-` if needed:

- `scripts/mediamtx_run.sh`
- `scripts/publish_xiao.sh`
- `mediamtx/mediamtx.yml`

Then:

```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'
./scripts/mediamtx_run.sh
```

Healthy log includes: `stream is available … 1 track (H264)` and `HLS … converting`.

## Watch

| Client | URL |
|--------|-----|
| Browser HLS | http://10.128.93.23:8888/cam_xiao/ |
| Browser WebRTC | http://10.128.93.23:8889/cam_xiao/ |
| VLC | `rtsp://10.128.93.23:8554/cam_xiao` |

## Optional

```bash
# Use long-lived MJPEG stream instead of capture polls
PUBLISH_MODE=stream ./scripts/mediamtx_run.sh

# Two-terminal debug
XIAO_EXTERNAL_PUBLISH=1 ./scripts/mediamtx_run.sh
./scripts/publish_xiao.sh http://10.128.93.25:81/stream
```

## What not to do

- Point MediaMTX `source:` at ESP MJPEG HTTP (unsupported).
- Use FFmpeg `-c copy` if you need browser HLS/WebRTC.
- Run a second `publish_xiao.sh` while `mediamtx_run.sh` already bridges (double publisher).
- Expect zero-gap forever from ESP MJPEG Wi‑Fi — restart blips are normal; true forever needs H.264-native hardware.
