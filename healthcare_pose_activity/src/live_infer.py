#!/usr/bin/env python3
"""
Live multi-person patient monitoring:
  - people count
  - pose (YOLO-pose skeleton)
  - activity per person (sit/stand/walk/lie/fall)
  - heuristic patient vs visitor/staff role
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from activity import ActivityTracker  # noqa: E402
from patients import PersonView, infer_role, summarize  # noqa: E402

ACTIVITY_COLOR = {
    "standing": (40, 180, 40),
    "sitting": (40, 180, 220),
    "walking": (220, 180, 40),
    "lying": (40, 40, 220),
    "fall_suspected": (0, 0, 255),
    "unknown": (120, 120, 120),
}
HUD_H = 88


def hud_safe_top() -> int:
    """Keep person tags below the top status bar."""
    return HUD_H + 4


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Patient room: count people + pose + activity"
    )
    ap.add_argument(
        "source",
        nargs="?",
        default="0",
        help="webcam index, RTSP URL, or HTTP MJPEG (default: 0)",
    )
    ap.add_argument("--model", default=None, help="pose weights (default yolo11n-pose.pt)")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--device", default="mps", help="mps | cpu | 0 (cuda)")
    ap.add_argument(
        "--no-track",
        action="store_true",
        help="disable ByteTrack IDs (per-frame only)",
    )
    return ap.parse_args()


def resolve_source(raw: str):
    return int(raw) if raw.isdigit() else raw


def pick_device(requested: str) -> str:
    try:
        import torch

        if (
            requested == "mps"
            and getattr(torch.backends, "mps", None)
            and torch.backends.mps.is_available()
        ):
            return "mps"
        if requested not in ("mps", "cpu") and torch.cuda.is_available():
            return requested
    except Exception:
        pass
    return "cpu" if requested == "mps" else requested


def main() -> None:
    args = parse_args()
    source = resolve_source(args.source)
    model_name = args.model or os.environ.get("POSE_MODEL", "yolo11n-pose.pt")
    device = pick_device(args.device)

    print("loading torch/ultralytics (first run can take ~30–60s)…", flush=True)
    from ultralytics import YOLO

    print(
        f"patient monitor | model={model_name} device={device} source={source}",
        flush=True,
    )
    print("loading pose weights…", flush=True)
    model = YOLO(model_name)
    print("opening camera / stream…", flush=True)
    # Persistent activity state per track id
    trackers: dict[int, ActivityTracker] = {}

    predict_kw = dict(
        source=source,
        stream=True,
        conf=args.conf,
        imgsz=args.imgsz,
        device=device,
        verbose=False,
        show=False,
        classes=[0],  # person only
    )

    use_ids = False
    if args.no_track:
        results_iter = model.predict(**predict_kw)
    else:
        try:
            import lap  # noqa: F401 — required by Ultralytics ByteTrack

            results_iter = model.track(
                **predict_kw, persist=True, tracker="bytetrack.yaml"
            )
            use_ids = True
        except Exception as e:
            print(f"track unavailable ({e}); falling back to predict", flush=True)
            results_iter = model.predict(**predict_kw)
            use_ids = False

    win = "Patient pose + activity (q quit)"
    t0 = time.time()
    n = 0
    try:
        for r in results_iter:
            n += 1
            # Skeleton + boxes only — no YOLO "id person 0.xx" text (avoids overlap)
            if r.boxes is not None and len(r.boxes):
                frame = r.plot(labels=False, conf=False, boxes=True, kpt_line=True)
            else:
                frame = r.orig_img.copy()
            people: list[PersonView] = []

            if (
                r.keypoints is not None
                and len(r.keypoints) > 0
                and r.boxes is not None
                and len(r.boxes) > 0
            ):
                kdata = r.keypoints.data.cpu().numpy()
                boxes = r.boxes.xyxy.cpu().numpy()
                ids = None
                if use_ids and r.boxes.id is not None:
                    ids = r.boxes.id.cpu().numpy().astype(int)

                areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                primary = int(np.argmax(areas)) if len(areas) else 0

                seen_ids: set[int] = set()
                for i in range(len(kdata)):
                    tid = int(ids[i]) if ids is not None else i
                    seen_ids.add(tid)
                    if tid not in trackers:
                        trackers[tid] = ActivityTracker()
                    st = trackers[tid].update(kdata[i])
                    box = tuple(float(x) for x in boxes[i])
                    role = infer_role(st, primary=(i == primary))
                    people.append(
                        PersonView(
                            track_id=tid,
                            activity=st,
                            role=role,
                            box=box,
                            area=float(areas[i]),
                        )
                    )

                    x1, y1, x2, y2 = map(int, boxes[i])
                    color = ACTIVITY_COLOR.get(st.label, (120, 120, 120))
                    # Single clean tag under HUD, above box (not stacked on YOLO text)
                    role_short = "patient" if role == "patient" else (
                        "other" if role == "visitor_or_staff" else "unk"
                    )
                    tag = f"ID{tid} {role_short} | {st.label} {st.confidence:.2f}"
                    (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
                    pad = 4
                    ty1 = max(hud_safe_top(), y1 - th - 2 * pad)
                    if ty1 < hud_safe_top():
                        ty1 = min(y2 + 2, frame.shape[0] - th - 2 * pad)
                    ty2 = ty1 + th + 2 * pad
                    tx2 = min(frame.shape[1] - 1, x1 + tw + 2 * pad)
                    cv2.rectangle(frame, (x1, ty1), (tx2, ty2), color, -1)
                    cv2.putText(
                        frame,
                        tag,
                        (x1 + pad, ty2 - pad - 2),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 255),
                        2,
                        cv2.LINE_AA,
                    )

                # Drop stale trackers
                for dead in [t for t in trackers if t not in seen_ids]:
                    # keep briefly — clear if idle many frames via simple prune
                    pass
                if len(trackers) > 32:
                    # hard cap
                    for dead in list(trackers.keys()):
                        if dead not in seen_ids:
                            del trackers[dead]

            scene = summarize(people)
            # HUD
            cv2.rectangle(frame, (0, 0), (min(frame.shape[1], 640), HUD_H), (20, 20, 20), -1)
            line1 = f"People: {scene.people_count}   Patients: {scene.patients}   Other: {scene.others}"
            line2 = f"Fall alerts: {scene.fall_alerts}"
            if scene.people:
                acts = ", ".join(
                    f"ID{p.track_id}:{p.activity.label}" for p in scene.people[:6]
                )
                line2 += f"   |  {acts}"
            cv2.putText(
                frame, line1, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2, cv2.LINE_AA
            )
            cv2.putText(
                frame, line2[:90], (12, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 220, 255), 2, cv2.LINE_AA
            )

            if scene.fall_alerts:
                cv2.putText(
                    frame,
                    "FALL SUSPECTED — check patient",
                    (12, HUD_H + 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.9,
                    (0, 0, 255),
                    2,
                    cv2.LINE_AA,
                )

            fps = n / max(1e-3, time.time() - t0)
            cv2.putText(
                frame,
                f"{fps:.1f} FPS",
                (frame.shape[1] - 110, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
                cv2.LINE_AA,
            )

            cv2.imshow(win, frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

            if n % 30 == 0:
                print(
                    f"people={scene.people_count} patients={scene.patients} "
                    f"falls={scene.fall_alerts} fps={fps:.1f}",
                    flush=True,
                )
    finally:
        cv2.destroyAllWindows()
        print(f"stopped after {n} frames", flush=True)


if __name__ == "__main__":
    main()
