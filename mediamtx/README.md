# MediaMTX (cameras → Mini → GPU)

The Mini pulls each camera, remuxes it, and MediaMTX serves it. A GPU pulls the RTSP URL over **TCP**. MediaMTX does not push.

```
camera (ESP Sense, or any IP camera)
        │
        ▼  publisher on the Mini (ffmpeg)
MediaMTX on 10.128.93.13
  RTSP  :8554/cam_sense, cam_sense2, …     GPU pulls this
  API   :9997/v3/paths/list                path list (Mini)
        │
        ▼
rtsp://10.128.93.13:8554/cam_sense
```

`mediamtx/docker/mediamtx.yml` follows `mycareapp-main/mediamtx.yml`: API on, `apiAddress: :9997`, RTSP TCP `:8554`, paths `all_others`. mycareapp calls the list on `http://127.0.0.1:9997/v3/paths/list` because MediaMTX runs on that machine. Here the same call is `http://10.128.93.13:9997/v3/paths/list`.



## Lab IPs

| Role | Address |
|------|---------|
| Mini (Ethernet) | `10.128.93.13` |
| Sense camera 1 | `10.128.93.15` → `cam_sense` (H.264 480×320 + AAC) |
| Sense camera 2 | `10.128.93.24` → `cam_sense2` (H.264 480×320 + AAC) |
| IP camera | `10.128.93.60` → `cam_sense3` (H.264 1920×1080, video only) |
| GPU `lnmcltappgke01` | `10.128.81.10` |
| GPU `lnmcltappgke02` | `10.128.81.11` |
| GPU `lnmcltappgke03` | `10.128.81.12` |
| GPU `lnmcltappgke04` | `10.128.81.13` |

Flash each board with **CameraWebServerWiFiSense** so both the picture and the mic are on:

```bash
./scripts/flash_camera_sense.sh /dev/cu.usbmodem…
```

## Always-on server (Docker)

MediaMTX runs in Docker (`bluenviron/mediamtx:1.21.0`) and stays up after a reboot. The publisher is a Mac login service: Docker Desktop on this Mini cannot open `10.128.93.x`, so the publisher runs on the Mini itself, restarts if it exits, and starts again at login. No terminal. The camera list is saved and published again when the service starts. `POST /cameras` replaces the whole list. Order picks the path: the first entry is `cam_sense`, then `cam_sense2`, and so on.

```bash
cd mediamtx/docker
docker compose up -d
./install_service.sh
```

Each entry is a camera IP or a stream URL. For an IP, the Mini looks for a working picture: common RTSP paths (port 554), common HTTP picture paths, and an ESP Sense board (`:81/stream` plus `/audio`). A posted `rtsp://` or `http://` URL is used as given. `http://IP/audio` is an ESP Sense board: picture and mic. The stream is republished as H.264, with audio when the camera sends it.

Path names follow the order of that POST:

| Position | Path |
|----------|------|
| 1st | `cam_sense` |
| 2nd | `cam_sense2` |
| 3rd | `cam_sense3` |
| 4th | `cam_sense4` |

`POST /cameras` replaces the saved list. Adding a 4th camera means sending all 4, new one last. Sending only the new IP drops the others. The same order next time keeps the same paths. The list is saved, so a restart publishes it again with no new POST.

An unplugged camera keeps its path. The other cameras stay up. Plug it back in and the same RTSP URL returns. No new POST. If the path stays down after the camera is reachable again, follow [If one path is down](#if-one-path-is-down).

```bash
# ESP Sense is http://IP/audio (picture + mic). A login camera is its rtsp:// URL.
# This replaces the whole set. Include every camera you want to keep.
curl -s -X POST http://127.0.0.1:8090/cameras \
  -H 'content-type: application/json' \
  -d '{"cameras":[
    "http://10.128.93.15/audio",
    "http://10.128.93.24/audio",
    "rtsp://USER:PASSWORD@10.128.93.60:554/profile2/media.smp",
    "http://NEW_IP/audio"
  ]}'
```

A camera that needs a login, or a custom path the search does not know:

```bash
curl -s -X POST http://127.0.0.1:8090/cameras \
  -H 'content-type: application/json' \
  -d '{"cameras":[
    "http://10.128.93.15/audio",
    {"ip":"192.168.1.50","user":"admin","password":"secret"},
    "rtsp://192.168.1.50:554/profile2/media.smp"
  ]}'
```

## Ports and APIs

| Port | Who calls it | What it is |
|------|----------------|------------|
| `8554` | GPU, VLC, laptop via tunnel | RTSP. Pull `rtsp://10.128.93.13:8554/<path>` over TCP. Open from `10.128.81.10`. |
| `9997` | Mini | MediaMTX API. Live or dead for the paths that are publishing. `GET /v3/paths/list` is every path. `GET /v3/paths/get/<name>` is one path. |
| `8090` | Mini localhost | Publisher. `POST /cameras` replaces the camera list. `GET /cameras` shows what was posted. |

MediaMTX listens on the Mini, `10.128.93.13`. A curl to `10.128.81.10`–`.13` port `9997` fails immediately: those addresses are GPUs, and nothing is listening there. From a GPU, port `9997` on the Mini times out. Port `8554` on the Mini is open. To use the path list from a GPU, allow TCP from `10.128.81.10`–`10.128.81.13` to `10.128.93.13` port `9997`.

`GET /v3/paths/list` is every live path (name, online, codecs). `GET /v3/paths/get/<name>` is one path. That is the same MediaMTX call as mycareapp (`http://127.0.0.1:9997/v3/paths/list` on the machine where MediaMTX runs). Adding cameras is port `8090`, Mini only.

On the Mini, one camera is live when the call returns `200` and `"online": true`. `404` means that path is not publishing:

```bash
curl -s http://127.0.0.1:9997/v3/paths/get/cam_sense
curl -s http://127.0.0.1:9997/v3/paths/list
curl -s http://127.0.0.1:8090/cameras
```

From a GPU, check the stream on `8554`. A live camera prints video. A dead one returns 404:

```bash
ffprobe -rtsp_transport tcp rtsp://10.128.93.13:8554/cam_sense
```

From a laptop that can SSH to the Mini:

```bash
ssh dtilakenonalab@10.128.93.13 'curl -s http://127.0.0.1:9997/v3/paths/get/cam_sense'
```

| Camera | RTSP |
|--------|------|
| `10.128.93.15` | `rtsp://10.128.93.13:8554/cam_sense` |
| `10.128.93.24` | `rtsp://10.128.93.13:8554/cam_sense2` |
| `10.128.93.60` | `rtsp://10.128.93.13:8554/cam_sense3` |
| next POST entry | `rtsp://10.128.93.13:8554/cam_sense4` |

GPU, play over TCP:

```bash
ffplay -rtsp_transport tcp rtsp://10.128.93.13:8554/cam_sense
```

Laptop, tunnel through the GPU, then play the local port:

```bash
ssh -N -L 18554:10.128.93.13:8554 USER@10.128.81.10
ffplay -rtsp_transport tcp rtsp://127.0.0.1:18554/cam_sense
```

Same host and local port for `cam_sense2`, `cam_sense3`, and `cam_sense4`. Leave the `ssh -N` terminal open while you play. `18554` exists only on the laptop. On the Mini, play `rtsp://127.0.0.1:8554/<path>`. VLC: open that URL and set RTP over RTSP (TCP).

mycareapp’s file publisher uses `rtsp://127.0.0.1:8554/test/video1`. These lab cameras are the live sources instead. DeepStream on a GPU registers `rtsp://10.128.93.13:8554/cam_sense` (and `cam_sense2`, `cam_sense3`) as `camera_url`.

## If one path is down

Do not POST the camera list again. Do not stop MediaMTX. Check that one path on the Mini (`200` is live, `404` is not publishing):

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9997/v3/paths/get/PATH
```

`PATH` is `cam_sense`, `cam_sense2`, and so on. If it is `404`, check the camera from the Mini. An ESP Sense board is port `81`. An RTSP camera is port `554`:

```bash
nc -vz -w 3 CAMERA_IP PORT
```

If `nc` fails, the camera is off the network. Plug it back in and wait. The same path comes back with no new POST.

If nothing is listening on `8090`, the publisher is stopped. The log is `~/Library/Application Support/mediamtx-cameras/data/publisher.log`. Start it again and do not POST:

```bash
lsof -nP -iTCP:8090 -sTCP:LISTEN
open -g -a "$HOME/Applications/MediaMTX Cameras.app"
```

If that app is already stuck, start the script in the SSH session and leave it running:

```bash
cd "$HOME/Library/Application Support/mediamtx-cameras"
./run_publisher.sh
```

If `nc` succeeds, port `8090` is open, and the path is still `404`, that camera’s `ffmpeg` is stuck. Stop only that process. The publisher starts a new one. The other cameras stay up:

```bash
pgrep -lf PATH
kill PID
```

Wait a few seconds and curl the path again. `200` means it is publishing. On the Mini, play `rtsp://127.0.0.1:8554/PATH`. On a laptop, play `rtsp://127.0.0.1:18554/PATH` only while the SSH tunnel is open. A GPU plays `rtsp://10.128.93.13:8554/PATH`.

Stop the server: `docker compose down` from `mediamtx/docker`. Do not stop it just to add or replug a camera.

## One-shot script

Same ESP paths, in the foreground, without Docker. Needs `brew install mediamtx ffmpeg` on the Mini.

```bash
# both live Sense boards (audio + video)
SENSE_AV_URLS=http://10.128.93.15,http://10.128.93.24 ./scripts/mediamtx_run.sh

# one board
SENSE_AV_URL=http://10.128.93.15 ./scripts/mediamtx_run.sh

# video-only firmware (CameraWebServerWiFi, no /audio) → cam_xiao, cam_xiao2, …
./scripts/mediamtx_run.sh http://10.128.93.25:81/stream
```

Sense paths are `cam_sense`, `cam_sense2`, …. Video-only paths are `cam_xiao`, `cam_xiao2`, …. Stop with Ctrl+C. If port 8554 is busy: `pkill -f mediamtx`.

The shell publisher also tees speech PCM to `udp://127.0.0.1:19055` for Whisper. The Docker publisher does not; it only serves H.264 + AAC on RTSP. Whisper setup: [`firmware/CameraWebServerWiFiSense/README.md`](../firmware/CameraWebServerWiFiSense/README.md#live-voice-recognition-whisper-on-mini).

## Watch

GPU: `ffplay -rtsp_transport tcp rtsp://10.128.93.13:8554/cam_sense`

Laptop: the SSH tunnel above, then `rtsp://127.0.0.1:18554/cam_sense`

VLC: Open Network → that RTSP URL → **RTP over RTSP (TCP)**, and turn the audio track on. `cam_sense3` is video only.

## Record

While a path is publishing, save it with stream-copy (H.264, plus AAC when the camera has audio). Any machine that can open port `8554` can record. That includes the Mini and a GPU node. Ctrl-C finishes a playable MP4.

On the Mini, from this repo:

```bash
# cam_sense, 120 seconds, recordings/cam_sense-<time>.mp4
./mediamtx/record.sh

# the README example
./mediamtx/record.sh cam_sense -o recordings/sense-demo.mp4 -t 120

# second camera
./mediamtx/record.sh cam_sense2 -t 60
```

On a GPU, `ffmpeg` is enough. The file is written on that GPU:

```bash
ffmpeg -rtsp_transport tcp -i rtsp://10.128.93.13:8554/cam_sense \
  -t 120 -map 0:v:0 -map 0:a:0? -c copy cam_sense.mp4
```

Use `cam_sense2` or `cam_sense3` the same way. `cam_sense3` has no audio; the optional audio map still writes a video file.

## Files

| File | Role |
|------|------|
| `mediamtx/docker/` | Always-on MediaMTX + publisher (`POST /cameras` on `:8090`) |
| `mediamtx/docker/mediamtx.yml` | Server config (API `:9997`, RTSP `:8554`, `all_others`) |
| `mediamtx/mediamtx.yml` | Base config for `./scripts/mediamtx_run.sh` |
| `mediamtx/mediamtx.runtime.yml` | Generated by the script (not committed) |
| `mediamtx/record.sh` | Record one path (video + audio) |
| `scripts/ffmpeg_sense_av.sh` | Shell remux used by `mediamtx_run.sh` |
