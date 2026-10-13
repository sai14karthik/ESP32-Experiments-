#!/usr/bin/env python3
"""
Live multi-person patient monitoring:
  - people count
  - pose (YOLO26-pose COCO-17 skeleton)
  - activity per person (sit/stand/walk/lie/fall)
  - heuristic patient vs visitor/staff role

Default model: YOLO26m-pose — best accuracy that still runs real-time on Apple MPS.
Official COCO pose mAP50-95: n=57.2  s=63.0  m=68.8  l=70.4  x=71.6
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

from activity import ALL_ACTIVITIES, ActivityTracker  # noqa: E402
from filters import draw_person, filter_person_indices  # noqa: E402
from mjpeg import is_mjpeg_url, is_rtsp_url, iter_mjpeg, iter_rtsp  # noqa: E402
from patients import PersonView, infer_role, summarize  # noqa: E402

ACTIVITY_COLOR = {
    "standing": (40, 180, 40),
    "sitting": (40, 180, 220),
    "walking": (220, 180, 40),
    "lying": (40, 40, 220),
    "fall_suspected": (0, 0, 255),
    "unknown": (120, 120, 120),
}
assert set(ALL_ACTIVITIES) <= set(ACTIVITY_COLOR), "missing activity colors"
HUD_H = 110

# Ultralytics YOLO26-pose quality presets (COCO Keypoints mAP50-95 e2e)
QUALITY = {
    "fast": {
        "model": "yolo26n-pose.pt",
        "imgsz": 512,
        "conf": 0.40,
        "map": "57.2",
    },
    "balanced": {
        "model": "yolo26s-pose.pt",
        "imgsz": 640,
        "conf": 0.42,
        "map": "63.0",
    },
    # Default: max useful accuracy for live webcam / room monitoring
    "accurate": {
        "model": "yolo26m-pose.pt",
        "imgsz": 640,
        "conf": 0.40,
        "map": "68.8",
    },
    # Slightly higher mAP; slower — use when FPS headroom exists
    "max": {
        "model": "yolo26l-pose.pt",
        "imgsz": 640,
        "conf": 0.38,
        "map": "70.4",
    },
}
DEFAULT_QUALITY = "accurate"


def hud_safe_top() -> int:
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
    ap.add_argument(
        "--quality",
        choices=tuple(QUALITY.keys()),
        default=os.environ.get("POSE_QUALITY", DEFAULT_QUALITY),
        help="fast|balanced|accurate(default)|max — model+imgsz preset",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="override pose weights (else from --quality / POSE_MODEL)",
    )
    ap.add_argument(
        "--conf",
        type=float,
        default=None,
        help="detection confidence (default from --quality)",
    )
    ap.add_argument("--imgsz", type=int, default=None, help="inference size")
    ap.add_argument("--device", default="mps", help="mps | cpu | 0 (cuda)")
    ap.add_argument(
        "--tracker",
        default="bytetrack.yaml",
        choices=("bytetrack.yaml", "botsort.yaml"),
        help="ByteTrack (faster) or BoT-SORT (stabler IDs)",
    )
    ap.add_argument(
        "--no-track",
        action="store_true",
        help="disable multi-object tracking (per-frame only)",
    )
    ap.add_argument(
        "--no-half",
        action="store_true",
        help="disable FP16 (slower, sometimes more stable on MPS)",
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


def resolve_profile(args: argparse.Namespace) -> dict:
    q = QUALITY[args.quality]
    model = (
        args.model
        or os.environ.get("POSE_MODEL")
        or q["model"]
    )
    return {
        "model": model,
        "imgsz": int(args.imgsz or q["imgsz"]),
        "conf": float(args.conf if args.conf is not None else q["conf"]),
        "map": q["map"],
        "quality": args.quality,
    }


def use_half(device: str, disabled: bool) -> bool:
    if disabled:
        return False
    # FP16: CUDA yes; MPS often flaky / deprecated path in ultralytics — skip MPS
    return device in ("0", "cuda") or (
        isinstance(device, str) and device.isdigit()
    )


def _infer_kwargs(profile: dict, device: str, half: bool) -> dict:
    kw = dict(
        conf=profile["conf"],
        imgsz=profile["imgsz"],
        device=device,
        verbose=False,
        show=False,
        classes=[0],
        max_det=10,
        nms=False,
    )
    if half:
        kw["half"] = True
    return kw


def main() -> None:
    args = parse_args()
    source = resolve_source(args.source)
    profile = resolve_profile(args)
    model_name = profile["model"]
    device = pick_device(args.device)
    half = use_half(device, args.no_half)

    print("loading torch/ultralytics (first run can take ~30–90s)…", flush=True)
    from ultralytics import YOLO

    print(
        f"patient monitor | quality={profile['quality']} "
        f"model={model_name} (COCO pose mAP≈{profile['map']}) "
        f"imgsz={profile['imgsz']} conf={profile['conf']} "
        f"device={device} half={half} source={source}",
        flush=True,
    )
    model = YOLO(model_name)
    if getattr(model, "task", None) != "pose":
        raise SystemExit(
            f"Expected a pose model (*-pose.pt), got task={model.task!r} for {model_name}"
        )
    try:
        model.fuse()
    except Exception as e:
        print(f"fuse skipped ({e})", flush=True)

    infer_kw = _infer_kwargs(profile, device, half)
    # Warmup
    try:
        dummy = np.zeros((profile["imgsz"], profile["imgsz"], 3), dtype=np.uint8)
        _ = model.predict(dummy, **infer_kw)
        print("warmup done", flush=True)
    except Exception as e:
        print(f"warmup skipped ({e}); continuing", flush=True)
        infer_kw.pop("half", None)
        infer_kw.pop("nms", None)

    trackers: dict[int, ActivityTracker] = {}
    want_track = not args.no_track
    use_ids = False
    if want_track:
        try:
            import lap  # noqa: F401

            use_ids = True
            print(f"tracker={args.tracker}", flush=True)
        except Exception as e:
            print(f"track unavailable ({e}); per-frame IDs", flush=True)
            use_ids = False

    # Frame pullers for ESP MJPEG / MediaMTX RTSP (Ultralytics stream often stalls)
    mjpeg_mode = is_mjpeg_url(source)
    rtsp_mode = is_rtsp_url(source)
    frame_mode = mjpeg_mode or rtsp_mode
    results_iter = None
    if mjpeg_mode:
        print(f"mjpeg reader: {source}", flush=True)
    elif rtsp_mode:
        print(f"rtsp reader (TCP): {source}", flush=True)
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    else:
        predict_kw = dict(source=source, stream=True, **infer_kw)

        def _start_stream(kw: dict, track: bool):
            if track:
                return (
                    model.track(
                        **kw, persist=True, tracker=args.tracker
                    ),
                    True,
                )
            return model.predict(**kw), False

        try:
            results_iter, use_ids = _start_stream(predict_kw, use_ids)
        except TypeError:
            predict_kw.pop("nms", None)
            results_iter, use_ids = _start_stream(predict_kw, use_ids)
        except Exception as e:
            print(f"track/predict failed ({e}); retry", flush=True)
            predict_kw.pop("nms", None)
            try:
                results_iter, use_ids = _start_stream(predict_kw, use_ids)
            except Exception as e2:
                print(f"track unavailable ({e2}); predict only", flush=True)
                results_iter, use_ids = _start_stream(predict_kw, False)

    win = "Patient pose + activity (q quit)"
    t0 = time.time()
    n = 0

    def handle_result(r) -> bool:
        """Annotate one YOLO result; return False if user quit."""
        nonlocal n
        n += 1
        frame = r.orig_img.copy()
        people: list[PersonView] = []
        fh, fw = frame.shape[:2]
        counts = {lab: 0 for lab in ALL_ACTIVITIES}

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

            keep = filter_person_indices(kdata, boxes, (fh, fw))
            if keep:
                areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                primary_i = max(keep, key=lambda j: float(areas[j]))
                seen_ids: set[int] = set()
                for i in keep:
                    tid = int(ids[i]) if ids is not None else i
                    seen_ids.add(tid)
                    if tid not in trackers:
                        trackers[tid] = ActivityTracker()
                    box = tuple(float(x) for x in boxes[i])
                    st = trackers[tid].update(
                        kdata[i], box=box, frame_hw=(fh, fw)
                    )
                    role = infer_role(st, primary=(i == primary_i))
                    people.append(
                        PersonView(
                            track_id=tid,
                            activity=st,
                            role=role,
                            box=box,
                            area=float(areas[i]),
                        )
                    )
                    color = ACTIVITY_COLOR.get(st.label, (120, 120, 120))
                    draw_person(frame, boxes[i], kdata[i], color)
                    x1, y1, x2, y2 = map(int, boxes[i])
                    role_short = (
                        "patient"
                        if role == "patient"
                        else ("other" if role == "visitor_or_staff" else "unk")
                    )
                    tag = f"ID{tid} {role_short} | {st.label} {st.confidence:.2f}"
                    (tw, th), _ = cv2.getTextSize(
                        tag, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2
                    )
                    pad = 4
                    ty1 = max(hud_safe_top(), y1 - th - 2 * pad)
                    if ty1 < hud_safe_top():
                        ty1 = min(y2 + 2, fh - th - 2 * pad)
                    ty2 = ty1 + th + 2 * pad
                    tx2 = min(fw - 1, x1 + tw + 2 * pad)
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
                if len(trackers) > 32:
                    for dead in list(trackers.keys()):
                        if dead not in seen_ids:
                            del trackers[dead]

        scene = summarize(people)
        for p in scene.people:
            counts[p.activity.label] = counts.get(p.activity.label, 0) + 1

        cv2.rectangle(
            frame, (0, 0), (min(frame.shape[1], 720), HUD_H), (20, 20, 20), -1
        )
        line1 = (
            f"People: {scene.people_count}   Patients: {scene.patients}   "
            f"Other: {scene.others}"
        )
        line2 = (
            f"stand:{counts['standing']} sit:{counts['sitting']} "
            f"walk:{counts['walking']} lie:{counts['lying']} "
            f"fall:{counts['fall_suspected']}"
        )
        if scene.people:
            line3 = ", ".join(
                f"ID{p.track_id}:{p.activity.label}" for p in scene.people[:5]
            )
        else:
            line3 = f"{model_name} | standing|sitting|walking|lying|fall  (q quit)"
        cv2.putText(
            frame, line1, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA
        )
        cv2.putText(
            frame, line2, (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 220, 255), 2, cv2.LINE_AA
        )
        cv2.putText(
            frame, line3[:95], (12, 84), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 255, 180), 1, cv2.LINE_AA
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
            return False
        if n % 30 == 0:
            acts = {lab: counts[lab] for lab in ALL_ACTIVITIES if counts[lab]}
            print(
                f"people={scene.people_count} patients={scene.patients} "
                f"acts={acts} falls={scene.fall_alerts} fps={fps:.1f}",
                flush=True,
            )
        return True

    def _infer_frame(frame: np.ndarray):
        try:
            if use_ids:
                return model.track(
                    frame, persist=True, tracker=args.tracker, **infer_kw
                )
            return model.predict(frame, **infer_kw)
        except TypeError:
            infer_kw.pop("nms", None)
            if use_ids:
                return model.track(
                    frame, persist=True, tracker=args.tracker, **infer_kw
                )
            return model.predict(frame, **infer_kw)

    try:
        if frame_mode:
            frames = (
                iter_mjpeg(str(source))
                if mjpeg_mode
                else iter_rtsp(str(source))
            )
            print("opening stream (first frame can take a few seconds)…", flush=True)
            for frame in frames:
                out = _infer_frame(frame)
                if not out:
                    continue
                if not handle_result(out[0]):
                    break
        else:
            assert results_iter is not None
            for r in results_iter:
                if not handle_result(r):
                    break
    finally:
        cv2.destroyAllWindows()
        print(f"stopped after {n} frames", flush=True)


if __name__ == "__main__":
    main()
