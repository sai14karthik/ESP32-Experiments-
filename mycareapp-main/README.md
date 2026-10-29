# mycareapp

Isolated DeepStream + Triton workspace. GPU-specific `.plan` files are rebuilt
at service startup from the model files mounted at `/models`.

## Quick start

Set up local configuration:

```sh
cd "$(git rev-parse --show-toplevel)"
cp sample.env .env
$EDITOR .env
```

Start the full pipeline (Triton, DeepStream, MediaMTX, and the test video
publisher). Each Compose service is profile-scoped and can also be started
independently:

```sh
docker compose --env-file .env \
  --profile triton --profile deepstream \
  --profile mediamtx --profile video-stream up --build -d
```

Or start services individually in order (see Startup order below).

`scripts/start_triton.sh` starts Triton only. `scripts/start_project.sh` waits
for Triton readiness and starts `deepstream_mycare_app`.

## Startup order

Depending on what you want to do, start the services in this order:

| Goal | Start order |
|---|---|
| Test video publisher only | `mediamtx` → `video-stream` |
| Full pipeline with test videos | `triton` → `deepstream` → `mediamtx` → `video-stream` |
| Full pipeline with a manual source | `triton` → `deepstream` → `mediamtx` → manual `ffmpeg` |
| Record or relay DeepStream output | DeepStream must already be running → `ffmpeg` |

To start each service:

```sh
# Triton
docker compose --env-file .env --profile triton up --build -d triton

# DeepStream (waits for Triton readiness internally)
docker compose --env-file .env --profile deepstream up --build -d deepstream

# MediaMTX (RTSP relay)
docker compose --env-file .env --profile mediamtx up -d mediamtx

# Test video publisher (loops videos/ into MediaMTX)
docker compose --env-file .env --profile video-stream up -d video-stream
```

MediaMTX and `video-stream` are independent: neither depends on the other, but
`video-stream` reconnects automatically once MediaMTX is up. The only runtime
dependency is DeepStream → Triton, which Compose does not couple with
`depends_on`; DeepStream waits for Triton readiness internally.

To stop one service without affecting the others, name the service:

```sh
docker compose --env-file .env --profile triton down triton
docker compose --env-file .env --profile deepstream down deepstream
docker compose --env-file .env --profile mediamtx down mediamtx
docker compose --env-file .env --profile video-stream down video-stream
```

## Runtime modes

- `TRITON_EXTERNAL=1` (default): Triton runs as a standalone Compose service
  and DeepStream connects to it.
- `TRITON_EXTERNAL=0`: DeepStream starts its own Triton process (no need to
  start the Triton service).

## Where commands run

- The workspace root on the host is mounted at `/rpm` inside the container.
- Run `docker compose ...` on the host.
- Run `scripts/create_model.sh`, `scripts/start_triton.sh`,
  `scripts/start_project.sh`, and the app inside the container.

To enter the running DeepStream container:

```sh
docker exec -it mycareapp bash
```

## Ports

| Service | Port |
|---|---:|
| Triton HTTP | `8010` |
| Triton gRPC | `8011` |
| Triton metrics | `8012` |
| DeepStream REST | `9011` |
| DeepStream RTSP | `8665` |
| DeepStream UDP | `5501` |
| MediaMTX RTSP | `8554` |
| MediaMTX API | `9997` |

Check Triton readiness:

```sh
curl -fsS http://localhost:8010/v2/health/ready
```

## Sources

### Naming convention

RTSP streams follow `rtsp://<host>:<port>/<prefix>/video{n}` where `{n}` is a
sequential number assigned in alphabetical order of the source filenames.

| Source file | RTSP path |
|---|---|
| `videos/input.mp4` | `rtsp://127.0.0.1:8554/test/video1` |
| `videos/parking_lot.mp4` | `rtsp://127.0.0.1:8554/test/video2` |

The `test` prefix separates development content from other namespaces (such as
`live` for relayed output). Configure the prefix with `STREAM_PREFIX` in
`.env`.

### Automatic publisher

The publisher scans `videos/` and pushes each video as an independent looping
RTSP stream to MediaMTX. Supported formats: `.mp4`, `.mkv`, `.avi`, `.mov`.

Start it (MediaMTX must already be running):

```sh
docker compose --env-file .env --profile video-stream up -d video-stream
```

Verify the published paths:

```sh
curl -s http://127.0.0.1:9997/v3/paths/list | python3 -m json.tool
```

Play a published stream:

```sh
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/test/video1
```

### Manual publishing

Start MediaMTX:

```sh
docker compose --env-file .env --profile mediamtx up -d mediamtx
```

Open a shell in the running DeepStream container and publish a looping video:

```sh
ffmpeg -re -stream_loop -1 -i /rpm/videos/input.mp4 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -g 30 -keyint_min 30 -sc_threshold 0 \
  -x264-params "repeat-headers=1" -an \
  -f rtsp -rtsp_transport tcp \
  rtsp://127.0.0.1:8554/test/video1
```

Register it with DeepStream:

```sh
curl -X POST http://127.0.0.1:9011/api/v1/stream/add \
  -H 'Content-Type: application/json' \
  --data '{"key":"sensor","value":{"camera_id":"video-1","camera_name":"recorded clip","camera_url":"rtsp://127.0.0.1:8554/test/video1","change":"camera_add","metadata":{"resolution":"1920x1080","codec":"h264","framerate":30}},"headers":{"source":"vst","created_at":"2026-10-27T00:00:00.000Z"}}'
```

## Tests

Manual test scripts for validating deployed models. See `tests/README.md`.

```sh
# Test Triton connectivity and list models
bash tests/test_triton.sh

# Test a specific model (e.g., YOLO after deployment)
bash tests/test_triton.sh yolo

# Run pose detection on a few frames from videos/input.mp4
uv run tests/test_pose_detection.py
```

Annotated frames with pose points are saved under `tests/results/annotated/`.

## Record or relay output

Read or relay through MediaMTX on `8554`. Record directly from DeepStream on
`8665`.

Record the annotated output:

```sh
ffmpeg -rtsp_transport tcp \
  -i rtsp://127.0.0.1:8665/ds-test \
  -an -c:v copy \
  -f matroska \
  annotated.mkv
```

Prefer Matroska for recordings because it remains playable if `ffmpeg` is
interrupted. Use MP4 only with a fixed duration so `ffmpeg` exits cleanly:

```sh
ffmpeg -rtsp_transport tcp \
  -i rtsp://127.0.0.1:8665/ds-test \
  -t 60 -an -c:v copy \
  annotated.mp4
```

Relay to MediaMTX on a different path:

```sh
ffmpeg -rtsp_transport tcp \
  -i rtsp://127.0.0.1:8665/ds-test \
  -c:v copy -an \
  -f rtsp -rtsp_transport tcp \
  rtsp://127.0.0.1:8554/live/annotated
```

## Troubleshooting

- `Connection refused`: MediaMTX is not listening on `8554`.
- `404 Not Found`: DeepStream is not publishing on `8665`, or the RTSP port is
  owned by MediaMTX.
- `batch-size must be <= 1`: preprocessing sent a missing batch dimension.
- Source reaches EOS: use `deepstream_mycare_app` and ensure `file-loop=1`
  under `[tests]`.
