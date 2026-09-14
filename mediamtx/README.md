# MediaMTX lab

Local proof that **viewers talk to MediaMTX**, not to the camera board.

| Path | Source |
|------|--------|
| `cam1` | Mac webcam (Stage 1) |
| `cam_xiao` | XIAO MJPEG via FFmpeg bridge (Stage 2) |

## How the pipeline works

There are two steps: **publish** (get video into MediaMTX) and **watch** (pick a protocol for your client).

```
XIAO / webcam  →  FFmpeg (publish)  →  MediaMTX  →  viewer (you choose)
                     RTSP in              cam1 /         RTSP / HLS / WebRTC
                                          cam_xiao
```

- **Publish** always uses **RTSP** — the `publish_*.sh` scripts handle this; you do not pick a protocol.
- **Watch** is where you choose: VLC → RTSP, browser (easy) → HLS, browser (lower delay) → WebRTC.

Direct ESP access (no MediaMTX): open `http://<xiao-ip>/` in a browser (MJPEG on port 80 / `:81/stream`).

**Firmware for Stage 2:** flash [`firmware/CameraWebServerWiFi`](../firmware/CameraWebServerWiFi/) (video-only). Do **not** need `CameraWebServerWiFiSense` (cam+mic+CSI) for MediaMTX — same `:81/stream` URL if you did flash Sense, but video-only is the safe MediaMTX target.

## Protocols — when to use which

| Protocol | Port | Use when | Example URL (`cam_xiao`) |
|----------|------|----------|--------------------------|
| **RTSP** | 8554 | VLC, ffplay, OBS, tools that expect `rtsp://` | `rtsp://127.0.0.1:8554/cam_xiao` |
| **HLS** | 8888 | Browser; OK with a few seconds of delay | http://127.0.0.1:8888/cam_xiao/ |
| **WebRTC** | 8889 | Browser; lowest latency in this lab | http://127.0.0.1:8889/cam_xiao/ |
| **RTMP** | 1935 | Legacy encoders (optional; not used by our scripts) | `rtmp://127.0.0.1:1935/cam_xiao` |

Swap `cam_xiao` → `cam1` for the Mac webcam path.

### Quick decision

| Goal | What to do |
|------|------------|
| Set up / feed video in | Run `mediamtx_run.sh` + `publish_webcam.sh` or `publish_xiao.sh` |
| Watch in **VLC** | RTSP — File → Open Network |
| Watch in **browser** (simple) | HLS — open `:8888/.../` |
| Watch in **browser** (live feel) | WebRTC — open `:8889/.../` |
| Test ESP only (skip MediaMTX) | Browser → `http://<xiao-ip>/` |

### What each protocol is (short)

- **RTSP** — Standard IP-camera protocol. Best for desktop players. Browsers do not play RTSP natively.
- **HLS** — HTTP video chunks. Works in Safari/Chrome; slightly higher latency than WebRTC.
- **WebRTC** — Real-time browser streaming. Best browser latency for localhost; remote access needs extra setup (out of scope here).
- **RTMP** — Enabled in config but our lab publishes via RTSP only unless you add a separate RTMP publisher.

## Install (once)

```bash
brew install mediamtx ffmpeg
```

If Homebrew already started a background MediaMTX service, stop it so our lab config can bind the ports:

```bash
brew services stop mediamtx
```

Then use `./scripts/mediamtx_run.sh` (loads `mediamtx/mediamtx.yml` from this repo).

## Stage 1 — Mac webcam

**Terminal A — start MediaMTX**

```bash
./scripts/mediamtx_run.sh
```

**Terminal B — publish webcam**

```bash
./scripts/publish_webcam.sh
# If the wrong camera is picked:
# WEBCAM_DEVICE=1 ./scripts/publish_webcam.sh
```

macOS may prompt for **Camera** permission for the Terminal / ffmpeg — allow it.

**Watch** (pick one client)

| Client | Protocol | URL |
|--------|----------|-----|
| VLC | RTSP | `rtsp://127.0.0.1:8554/cam1` (File → Open Network…) |
| Browser | HLS | http://127.0.0.1:8888/cam1/ |
| Browser | WebRTC | http://127.0.0.1:8889/cam1/ |

```bash
# Optional: test RTSP from the terminal
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/cam1
```

Acceptance: video in VLC and browser; stop `publish_webcam.sh` → stream ends; restart → returns.

## Stage 2 — XIAO → all protocols (browsers included)

Board: flash `firmware/CameraRTSPWiFi` (serial shows `rtsp://10.128.93.25:8554/mjpeg/1`).

ESP outputs **MJPEG**; browsers need **H.264**. One command on the Mini does both:

```bash
./scripts/mediamtx_run.sh
# ESP RTSP → ffmpeg H.264 → MediaMTX → RTSP + HLS + WebRTC + RTMP
```

Do **not** also run `publish_xiao.sh` (MediaMTX already starts it via `runOnInit`).

**Watch** (pick one)

| Client | Protocol | URL (on Mini / colleague) |
|--------|----------|---------------------------|
| Browser | HLS | http://10.128.93.23:8888/cam_xiao/ |
| Browser | WebRTC | http://10.128.93.23:8889/cam_xiao/ |
| VLC | RTSP | `rtsp://10.128.93.23:8554/cam_xiao` |

Localhost variants use `127.0.0.1` instead of `10.128.93.23`.

Fallback if board still has CameraWebServerWiFi (HTTP MJPEG):

```bash
XIAO_RTSP_URL= ./scripts/mediamtx_run.sh
```

## End-to-end example (Mac Mini + XIAO on LabPSK)

```bash
# One terminal on Mini (after syncing this repo)
./scripts/mediamtx_run.sh
```

Then open **one** viewer URL:

- Browser (HLS): http://10.128.93.23:8888/cam_xiao/
- Browser (WebRTC): http://10.128.93.23:8889/cam_xiao/
- VLC: `rtsp://10.128.93.23:8554/cam_xiao`


## Ports (localhost)

| Port | Protocol | Role in this lab |
|------|----------|------------------|
| 8554 | RTSP | FFmpeg **publishes** here; VLC **plays** from here |
| 8888 | HLS | Browser playback |
| 8889 | WebRTC | Browser playback (lower latency) |
| 1935 | RTMP | Available; not used by default scripts |

## Config

[`mediamtx/mediamtx.yml`](mediamtx.yml) — lab only (no auth). Paths use `source: publisher` so FFmpeg pushes in.

## Out of scope here

Hospital VLANs, TLS/auth, GCP, recording, CSI pipeline.
