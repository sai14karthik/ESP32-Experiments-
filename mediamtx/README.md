# MediaMTX (cameras → Mini → GPU)

The Mini reads each camera and sends its video to MediaMTX. A GPU then reads the video from MediaMTX over RTSP/TCP. MediaMTX does not send video to the GPU by itself. RTSP viewing is read-only without a login; restrict port `8554` to trusted GPU and operator machines with the lab firewall.

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

The existing [root MediaMTX config](../mediamtx.yml) is used by the main Compose setup for local test videos. The config in `mediamtx/docker/` is for the separate lab Mini camera server. They look similar, but serve different deployments and have different access settings, so both are kept.

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

## Always-on server (Docker)

MediaMTX runs in Docker (`bluenviron/mediamtx:1.21.0`) and stays up after a reboot. The publisher is a Mac login service: Docker Desktop on this Mini cannot open `10.128.93.x`, so the publisher runs on the Mini itself, restarts if it exits, and starts again at login. No terminal. The camera list is saved and published again when the service starts. `POST /cameras` replaces the whole list. Order picks the path: the first entry is `cam_sense`, then `cam_sense2`, and so on.

From the repository root, install and start the publisher service:

```bash
./mediamtx/docker/install_service.sh
```

The installer starts the MediaMTX Docker container, creates private publisher and API passwords, copies the publisher files into macOS Application Support, and sets the publisher to start at login. The credentials are saved in `~/Library/Application Support/mediamtx-cameras/credentials.env` with owner-only permissions; do not share or commit that file. To start only MediaMTX manually, first run `./mediamtx/docker/install_service.sh`, then run `docker compose --env-file "$HOME/Library/Application Support/mediamtx-cameras/credentials.env" -f mediamtx/docker/docker-compose.yml up -d mediamtx` from the repository root.

Each entry is a camera IP or a stream URL. For an IP, the Mini looks for a working picture: common RTSP paths (port 554), common HTTP picture paths, and an ESP Sense board (`:81/stream` plus `/audio`). A posted `rtsp://` or `http://` URL is used as given. `http://IP/audio` is an ESP Sense board: picture and mic. The stream is republished as H.264, with audio when the camera sends it.

The publisher API needs the generated username and password. Load them in the Mini terminal before making API calls:

```bash
set -a
source "$HOME/Library/Application Support/mediamtx-cameras/credentials.env"
set +a
```

Path names follow the order of that POST:

| Position | Path |
|----------|------|
| 1st | `cam_sense` |
| 2nd | `cam_sense2` |
| 3rd | `cam_sense3` |
| 4th | `cam_sense4` |

`POST /cameras` replaces the saved list only when every entry is valid. If discovery fails for any bare IP, the request is rejected and the active list stays unchanged. Explicit stream URLs are used as supplied; if a camera is temporarily unreachable, its publisher retries until it returns. Adding a 4th camera means sending all 4, new one last. Sending only the new IP replaces the old list with that one camera. The same order next time keeps the same paths. The list is saved, so a restart publishes it again with no new POST. An empty list deliberately stops all publishers.

An unplugged camera keeps its path. The other cameras stay up. Plug it back in and the same RTSP URL returns. No new POST. If the path stays down after the camera is reachable again, follow [If one path is down](#if-one-path-is-down).

```bash
# ESP Sense is http://IP/audio (picture + mic). A login camera is its rtsp:// URL.
# This replaces the whole set. Include every camera you want to keep.
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" \
  -X POST http://127.0.0.1:8090/cameras \
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
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" \
  -X POST http://127.0.0.1:8090/cameras \
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
| `8554` | GPU, VLC, laptop via tunnel | RTSP. Clients can read streams without credentials; only the publisher has write access. Restrict network access to trusted devices. |
| `9997` | Mini localhost | MediaMTX control API. Host port is loopback-only and API calls require the generated admin credentials. |
| `8090` | Mini localhost | Camera publisher API. Bound to loopback and requires the generated credentials; `POST /cameras` replaces the list only after validating every entry. Status responses omit camera source URLs and credentials. |

MediaMTX listens on the Mini, `10.128.93.13`. The control API is intentionally not exposed to the LAN, so do not open port `9997` in the firewall. To check a path remotely, create an SSH tunnel to the Mini and use the admin credentials locally through that tunnel. RTSP port `8554` must be reachable by the GPU; restrict it to trusted GPU hosts at the firewall.

Load the generated API credentials in a Mini terminal before calling the MediaMTX API:

```bash
set -a
source "$HOME/Library/Application Support/mediamtx-cameras/credentials.env"
set +a
```

`GET /v3/paths/list` is every live path (name, online, codecs). `GET /v3/paths/get/<name>` is one path. Adding cameras uses port `8090`, which is available only on the Mini itself.

On the Mini, one camera is live when the call returns `200` and `"online": true`. `404` means that path is not publishing:

```bash
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" http://127.0.0.1:9997/v3/paths/get/cam_sense
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" http://127.0.0.1:9997/v3/paths/list
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" http://127.0.0.1:8090/cameras
```

From a GPU, check the stream on `8554`. A live camera prints video. A dead one returns 404:

```bash
ffprobe -rtsp_transport tcp rtsp://10.128.93.13:8554/cam_sense
```

From a laptop that can SSH to the Mini:

```bash
ssh USER@10.128.93.13 'set -a; source "$HOME/Library/Application Support/mediamtx-cameras/credentials.env"; set +a; curl -u "$MTX_API_USER:$MTX_API_PASSWORD" http://127.0.0.1:9997/v3/paths/get/cam_sense'
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
curl -s -u "$MTX_API_USER:$MTX_API_PASSWORD" -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9997/v3/paths/get/PATH
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

Stop the server with `docker compose --env-file "$HOME/Library/Application Support/mediamtx-cameras/credentials.env" -f mediamtx/docker/docker-compose.yml down` from the repository root. Do not stop it just to add or replug a camera.

## Watch

GPU: `ffplay -rtsp_transport tcp rtsp://10.128.93.13:8554/cam_sense`

Laptop: the SSH tunnel above, then `rtsp://127.0.0.1:18554/cam_sense`

VLC: Open Network → that RTSP URL → **RTP over RTSP (TCP)**, and turn the audio track on. `cam_sense3` is video only.

## Record a stream

While a path is live, save it as an MP4. The helper uses ffmpeg to copy the video and audio without re-encoding. Install ffmpeg on the machine that runs the command.

On the Mini, from this repo:

```bash
# Save cam_sense for the default 120 seconds.
./mediamtx/record.sh

# Save a named file for 120 seconds.
./mediamtx/record.sh cam_sense -o recordings/sense-demo.mp4 -t 120

# Save the second camera for 60 seconds.
./mediamtx/record.sh cam_sense2 -t 60
```

On a GPU, `ffmpeg` is enough. The file is written on that GPU:

```bash
ffmpeg -rtsp_transport tcp -i rtsp://10.128.93.13:8554/cam_sense \
  -t 120 -map 0:v:0 -map 0:a:0? -c copy cam_sense.mp4
```

Use `cam_sense2` or `cam_sense3` the same way. `cam_sense3` has no audio; the optional audio map still writes a video file.

## What the scripts and configs do

| File | In simple terms |
|------|-----------------|
| `mediamtx/docker/app.py` | Accepts the camera list, finds stream URLs when needed, starts ffmpeg publishers, and saves the list so it can be restored after restart. |
| `mediamtx/docker/run_publisher.sh` | Starts the camera-list service and restarts it if it stops. |
| `mediamtx/docker/install_service.sh` | Starts MediaMTX and installs the publisher as a macOS background service that starts at login. |
| `mediamtx/docker/Dockerfile` | Builds the optional Docker image for running the publisher inside Docker. |
| `mediamtx/record.sh` | Saves one live RTSP stream to an MP4 file using ffmpeg. |
| `mediamtx/docker/docker-compose.yml` | Starts the lab Mini's MediaMTX container. The optional `in-container` profile runs the publisher in Docker instead of on the Mac host. |
| `mediamtx/docker/mediamtx.yml` | Sets the lab Mini server's RTSP and API ports and allows publishers to create camera paths. |
| `../mediamtx.yml` | Configures the separate root Compose MediaMTX service used with local test videos. Keep this separate from the lab Mini config. |

The unused one-shot and webcam configurations were removed. The root Compose test-video workflow is separate from the lab camera publisher described here.
