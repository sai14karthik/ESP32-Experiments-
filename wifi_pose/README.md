# wifi_pose

One home for the Wi‑Fi → 3D pose app (CVPR-style): **GT collect now**, transformer train + live later.
Keeps SoftAP firmware / Reddit UI / other lab trees separate; this folder owns the pose product.

## Setup (once)

```bash
cd wifi_pose
uv sync
# pose model (if missing)
cp ../realsense_skeleton/pose_landmarker_lite.task models/
./scripts/install_nopasswd.sh   # macOS librealsense needs root
```

## Record a session

```bash
./scripts/run_record.sh --name walk1
./scripts/run_record.sh --name walk1 --save-color
./scripts/run_record.sh --name clip40 --max-seconds 40
```

Press `q` to stop. Output:

```
sessions/<YYYYMMDD_HHMMSS>_<name>/
  meta.json
  frames.jsonl      # t_wall_ns, t_mono_ns, people joints
  color/            # only with --save-color
  preview.jpg
```
