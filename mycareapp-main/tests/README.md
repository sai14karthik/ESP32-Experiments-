# Tests

Manual test scripts for validating deployed models and services.

## Usage

```sh
# Test Triton connectivity and list models
bash tests/test_triton.sh

# Test a specific model's readiness and metadata
bash tests/test_triton.sh yolo26_m_pose

# Run pose detection on a few frames from videos/input.mp4
uv run tests/test_pose_detection.py

# Run pose detection with a specific model
uv run tests/test_pose_detection.py yolo26_m_pose
```

`tests/test_triton.sh` runs on the host or inside a container with network
access to Triton. `tests/test_pose_detection.py` runs through `uv run`, which
resolves dependencies from the root `pyproject.toml` into a local `.venv/`.
It extracts evenly spaced frames from `videos/input.mp4`, sends them to
Triton, and saves the raw inference outputs to `tests/results/` (gitignored).
Annotated frames with pose points, skeleton connections, and bounding boxes
are saved under `tests/results/annotated/`.

The test uses the COCO 17-keypoint format. `KEYPOINT_NAMES` in
`tests/test_pose_detection.py` maps each keypoint index to its body part
(e.g., index 0 is `nose`, index 5 is `left_shoulder`). `SKELETON` defines the
connections grouped by body region (head, arms, torso, legs), and
`SKELETON_NAMES` resolves them to body part names.

Both scripts configure the HTTP port with `TRITON_HTTP_PORT` (default `8010`).
The pose detection test accepts `NUM_FRAMES` (default `5`), `DET_THRESHOLD`
(default `0.25`, matching Ultralytics) and `KPT_THRESHOLD` (default `0.5`,
matching Ultralytics).
