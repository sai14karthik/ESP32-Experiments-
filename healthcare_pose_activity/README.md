# Healthcare pose + activity (patient room)

Live **patient monitoring** on laptop/Mini (webcam or ESP → MediaMTX).

Not the left/right/up/down demo. Not GPU DeepStream RPM (`rpm-path2` / Skateformer).

## Targets

| Capability | Output |
|------------|--------|
| **People count** | How many people in frame |
| **Pose** | YOLO26-pose COCO-17 skeleton per person |
| **Activity** | `standing` / `sitting` / `walking` / `lying` / `fall_suspected` / `unknown` |
| **Role (heuristic)** | `patient` vs `visitor_or_staff` (posture — **not** face ID) |

**Not clinical diagnosis** — lab / monitoring prototype only.

## Quick start

```bash
cd healthcare_pose_activity

# Default: accurate = YOLO26m-pose (best live accuracy on Mac)
./run_live.sh 0

# Speed vs accuracy presets
POSE_QUALITY=fast ./run_live.sh 0       # yolo26n — max FPS
POSE_QUALITY=balanced ./run_live.sh 0   # yolo26s
POSE_QUALITY=accurate ./run_live.sh 0   # yolo26m (default)
POSE_QUALITY=max ./run_live.sh 0        # yolo26l — highest mAP, slower

# ESP via MediaMTX
./run_live.sh rtsp://127.0.0.1:18554/cam_sense
./run_live.sh rtsp://10.128.93.13:8554/cam_sense
```

Press **`q`**. Prefer **Terminal.app** for camera permission.

## Models (Ultralytics YOLO26-pose, COCO Keypoints)

| Preset | Weights | mAP50-95 | Use when |
|--------|---------|----------|----------|
| fast | `yolo26n-pose.pt` | 57.2 | max FPS / weak CPU |
| balanced | `yolo26s-pose.pt` | 63.0 | good FPS + OK keypoints |
| **accurate (default)** | **`yolo26m-pose.pt`** | **68.8** | **best live tradeoff** |
| max | `yolo26l-pose.pt` | 70.4 | FPS headroom; +1.6 over m |
| (override) | `yolo26x-pose.pt` | 71.6 | offline / not live |

Override: `POSE_MODEL=yolo26l-pose.pt ./run_live.sh 0`

## Stack

- **Pose:** YOLO26-pose + `model.fuse()` + FP16 on MPS/CUDA + ByteTrack IDs  
- **Activity:** literature-aligned geometry (aspect, knee/hip angles, centroid/scale velocity, temporal fall)  
- **Filters:** drop hand false-people; prefer detections with hips for activity  
- Hospital-grade action (PCT/Skateformer): MediaMTX `deepstream` / RPM on GPU  

## Layout

```
healthcare_pose_activity/
  README.md
  run_live.sh
  src/
    activity.py     # keypoints → all 6 activities
    patients.py     # count / patient role
    filters.py      # false-person filter + COCO skeleton
    live_infer.py   # live loop + quality presets
    test_activity.py
```

## Notes

- First `accurate` run downloads `yolo26m-pose.pt` (~minor wait).
- **Step back** so hips/knees are visible — required for sit/stand/walk.
- Stabler IDs: `./run_live.sh 0 --tracker botsort.yaml`
- If MPS half-precision glitches: `./run_live.sh 0 --no-half`
