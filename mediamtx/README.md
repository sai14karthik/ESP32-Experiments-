# MediaMTX lab (this Mac)

Local proof that **viewers talk to MediaMTX**, not to the camera board.

| Path | Source |
|------|--------|
| `cam1` | Mac webcam (Stage 1) |
| `cam_xiao` | XIAO MJPEG via FFmpeg bridge (Stage 2) |

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

**Watch**

| Client | URL |
|--------|-----|
| VLC | `rtsp://127.0.0.1:8554/cam1` (File → Open Network…) |
| Browser HLS | [http://127.0.0.1:8888/cam1/](http://127.0.0.1:8888/cam1/) |
| Browser WebRTC | [http://127.0.0.1:8889/cam1/](http://127.0.0.1:8889/cam1/) |

Acceptance: video in VLC and browser; stop `publish_webcam.sh` → stream ends; restart → returns.

## Stage 2 — XIAO MJPEG bridge

1. Flash / run `firmware/CameraWebServerWiFi` so the board serves MJPEG on the LAN.
2. Confirm in a browser: `http://<xiao-ip>/` (stream is often `:81/stream`).
3. Keep MediaMTX running, then:

```bash
./scripts/publish_xiao.sh http://<xiao-ip>:81/stream
```

**Watch**

| Client | URL |
|--------|-----|
| VLC | `rtsp://127.0.0.1:8554/cam_xiao` |
| Browser HLS | [http://127.0.0.1:8888/cam_xiao/](http://127.0.0.1:8888/cam_xiao/) |

`cam1` and `cam_xiao` can run at the same time.

## Ports (localhost)

| Port | Protocol |
|------|----------|
| 8554 | RTSP |
| 1935 | RTMP |
| 8888 | HLS |
| 8889 | WebRTC |

## Config

[`mediamtx/mediamtx.yml`](mediamtx.yml) — lab only (no auth). Paths use `source: publisher` so FFmpeg pushes in.

## Out of scope here

Hospital VLANs, TLS/auth, GCP, recording, CSI pipeline.
