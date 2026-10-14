# Healthcare pose + activity (patient room)

Live **patient monitoring** on laptop/Mini (webcam or ESP → MediaMTX).

Not the left/right/up/down demo. Not GPU DeepStream RPM (`rpm-path2` / Skateformer).

## Targets

| Capability | Output |
|------------|--------|
| **People count** | How many people in frame |
| **Pose** | YOLO26-pose COCO-17 skeleton per person |
| **Activity** | `standing` / `sitting` / `walking` / `lying` / `fall_suspected` / `unknown` |
| **All people** | Per-person activity labels and room-wide fall alerts; no role classes |

**Not clinical diagnosis** — lab / monitoring prototype only.

The pose model estimates body keypoints; activity labels are camera-dependent geometry and motion heuristics, not a separately trained action-recognition model. The published pose mAP numbers below do **not** measure sitting/walking/lying/fall accuracy. `unknown` is intentional when pose evidence is insufficient. Validate labels against manually reviewed clips from the actual camera, including occlusion, posture transitions, and intentional lying down, before relying on them.

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
./run_live.sh rtsp://127.0.0.1:18554/cam_sense --audio
# omit --audio for silent video
```

For a laptop that cannot directly reach the Mac Mini, start the SSH local forward on the laptop in another terminal and leave it running:

```bash
ssh -N -L 18554:10.128.93.13:8554 ojl1977@10.128.81.10
```

Then run the app against `rtsp://127.0.0.1:18554/cam_sense --audio`. This forwards laptop localhost → GPU SSH host → Mac Mini MediaMTX; video and the optional `ffplay` audio subscriber use the tunnel.

Press **`q`** to quit. The app reports activity for every detected person. Prefer **Terminal.app** for camera permission.

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

- **Pose:** YOLO26-pose + `model.fuse()` + FP16 on CUDA only; ByteTrack when available
- **Activity:** 2D pose geometry and motion heuristics; temporal walking and fall-like transition detection
- **Filters:** drop hand false-people; prefer detections with hips for activity  
- Hospital-grade action (PCT/Skateformer): MediaMTX `deepstream` / RPM on GPU  

## Layout

```
healthcare_pose_activity/
  README.md
  run_live.sh
  src/
    activity.py     # keypoints → all 6 activities
    patients.py     # per-person views / scene summary
    filters.py      # false-person filter + COCO skeleton
    live_infer.py   # live loop + quality presets
    test_activity.py
```

## Notes

- First `accurate` run downloads `yolo26m-pose.pt` (~minor wait).
- **Step back** so hips/knees are visible — required for sit/stand/walk.
- Track IDs are session-local, **not real-world identities**. Short tracking gaps are associated using bounding-box overlap, but crossings and longer occlusions can still swap IDs. Visitor/patient/doctor roles are not classified in this stage.
- `fall_suspected` requires an observed upright-to-low-posture transition with movement, remains active while the person stays down, and clears after sustained upright recovery. Lying down quickly can still resemble a fall; a missed/occluded fall can be missed. Treat this as an experimental alert, not a safety system or clinical assessment.
- `walking` is motion-based and can be confused with other body/track movement; it is not gait-validated. Sitting versus crouching/reclining and lying versus strong leaning remain ambiguous in 2D views.
- RTSP audio is opt-in with `--audio` and played by a separate `ffplay` subscriber while OpenCV displays analyzed video. This works with the MediaMTX A/V path (e.g. Mac Mini RTSP `cam_sense` reached via SSH tunnel); install FFmpeg/`ffplay` on the machine running this app. The ESP's raw `/audio` endpoint is not opened separately. Webcam and direct MJPEG sources remain video-only.
- Run the offline regression suite with `python src/test_activity.py` or `RUN_YOLO=0 python src/test_smoke.py`. YOLO smoke inference is opt-in with `RUN_YOLO=1` and may download weights.
- MPS uses full precision in this app. `--no-half` disables FP16 when running on CUDA.
