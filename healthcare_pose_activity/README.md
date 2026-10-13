# Healthcare pose + activity (patient room)

Separate project for **live patient monitoring** on laptop/Mini (ESP → MediaMTX or webcam).

Not the left/right/up/down demo. Not GPU DeepStream RPM (that stays `rpm-path2` / Skateformer).

## Targets (patient-focused)

| Capability | Output |
|------------|--------|
| **People count** | How many people in frame |
| **Pose** | YOLO-pose skeleton (COCO-17) per person |
| **Activity** | `standing` / `sitting` / `walking` / `lying` / `fall_suspected` / `unknown` |
| **Role (heuristic)** | `patient` vs `visitor_or_staff` (posture-based — **not** face ID) |

Patient-like: lying, sitting, fall; walking → visitor/staff; standing → patient if largest person else other.

**Not clinical diagnosis** — lab / monitoring prototype only.

## Quick start

```bash
cd healthcare_pose_activity

# Laptop webcam
./run_live.sh 0

# ESP via MediaMTX (tunnel or LabPSK)
./run_live.sh rtsp://127.0.0.1:18554/cam_sense
./run_live.sh rtsp://10.128.93.13:8554/cam_sense

# Direct ESP MJPEG
./run_live.sh http://10.128.93.17:81/stream
```

HUD shows **People / Patients / Other / Fall alerts** plus per-ID activity. Press **`q`**. Prefer **Terminal.app**.

## Stack (web-aligned)

- **Pose:** Ultralytics **YOLO26s-pose** (default) — COCO-17 keypoints; official skeleton draw  
  - Faster: `POSE_MODEL=yolo26n-pose.pt ./run_live.sh 0`
- **Activity (real-time, no heavy ST-GCN):** hybrid rules used in open YOLO-pose HAR projects + fall papers:
  - bbox aspect → lying (`w/h ≥ 1.5`) / upright
  - hip–knee–ankle & shoulder–hip–knee angles → sitting vs standing
  - normalized centroid velocity → walking (confirmed over several frames)
  - temporal upright→horizontal → `fall_suspected`
- **Not** left/right/up/down demos. GPU Skateformer path remains DeepStream RPM.
- ESP: MediaMTX RTSP/MJPEG sources  

## Layout

```
healthcare_pose_activity/
  README.md
  run_live.sh
  src/
    activity.py     # keypoints → activity (+ fall)
    patients.py     # people count / patient role summary
    live_infer.py   # live multi-person loop + overlay
```

## Notes

- First run downloads `yolo26s-pose.pt`.
- **Step back** so hips/knees are visible — required for accurate sit/stand/walk.
- `--no-track` if ByteTrack misbehaves.
- Production hospital action (PCT/Skateformer): MediaMTX `deepstream` / RPM on GPU.
