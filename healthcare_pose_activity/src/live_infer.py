#!/usr/bin/env python3
"""Live multi-person pose/activity monitoring with room-wide fall alerts.

YOLO26-pose estimates COCO-17 keypoints; activity labels are geometry and
motion heuristics, not a separately trained action-recognition model. No
patient/staff/doctor roles are inferred in this stage.

Default model: YOLO26m-pose. Published COCO pose mAP50-95: n=57.2, s=63.0,
m=68.8, l=70.4, x=71.6. Pose mAP does not measure activity accuracy.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from activity import ALL_ACTIVITIES, ActivityTracker  # noqa: E402
from filters import StablePersonIds, draw_person, filter_person_indices  # noqa: E402
from mjpeg import (  # noqa: E402
    VIDEO_SUFFIXES,
    is_rtsp_url,
    is_video_file,
    iter_mjpeg,
    iter_rtsp,
    iter_video_loop,
    source_mode,
)
from patients import PersonView, summarize  # noqa: E402

ACTIVITY_COLOR = {
    "standing": (40, 180, 40),
    "sitting": (40, 180, 220),
    "walking": (220, 180, 40),
    "lying": (40, 40, 220),
    "fall_suspected": (0, 0, 255),
    "unknown": (120, 120, 120),
}
assert set(ALL_ACTIVITIES) <= set(ACTIVITY_COLOR), "missing activity colors"

# Ultralytics YOLO26-pose quality presets (COCO Keypoints mAP50-95 e2e)
QUALITY = {
    "fast": {
        "model": "yolo26n-pose.pt",
        "imgsz": 512,
        "conf": 0.25,
        "map": "57.2",
    },
    "balanced": {
        "model": "yolo26s-pose.pt",
        "imgsz": 640,
        "conf": 0.25,
        "map": "63.0",
    },
    # Default: max useful accuracy for live webcam / room monitoring
    "accurate": {
        "model": "yolo26m-pose.pt",
        "imgsz": 640,
        "conf": 0.25,
        "map": "68.8",
    },
    # Slightly higher mAP; slower — use when FPS headroom exists
    "max": {
        "model": "yolo26l-pose.pt",
        "imgsz": 640,
        "conf": 0.25,
        "map": "70.4",
    },
}
DEFAULT_QUALITY = "accurate"


def view_layout(
    fw: int,
    fh: int,
    *,
    view_width: int = 0,
    view_scale: float = 0.0,
) -> dict:
    """
    Dynamic HUD + display scale for any source size
    (Sense 480×320, webcam 720p/1080p, RTSP, etc.).
    """
    # Status strip ~5% of frame height (never dominate the picture)
    hud_h = int(np.clip(round(fh * 0.062), 22, 60))
    font = float(np.clip(fh / 640.0, 0.38, 0.88))
    tag_font = float(np.clip(fh / 700.0, 0.40, 0.76))
    thick = 2 if fh >= 720 else 1
    status_y = max(14, int(hud_h * 0.72))

    if view_scale and view_scale > 0:
        scale = float(view_scale)
    elif view_width and view_width > 0:
        scale = float(view_width) / max(1, fw)
    else:
        # Auto: bump tiny ESP/MJPEG; leave HD alone; gently shrink ultra-wide
        if fw < 640:
            scale = 960.0 / fw
        elif fw < 900:
            scale = 1100.0 / fw
        elif fw > 1920:
            scale = 1600.0 / fw
        else:
            scale = 1.0

    return {
        "hud_h": hud_h,
        "font": font,
        "tag_font": tag_font,
        "thick": thick,
        "status_y": status_y,
        "scale": scale,
    }


def _fit_hud_lines(text: str, max_width: int, font: float, thick: int, max_lines: int) -> list[str]:
    """Break a HUD sentence into lines that fit the frame width."""
    words = text.split()
    if not words or max_lines < 1:
        return [text]
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word])
        (width, _height), _ = cv2.getTextSize(trial, cv2.FONT_HERSHEY_SIMPLEX, font, thick)
        if current and width > max_width and len(lines) < max_lines - 1:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current and len(lines) < max_lines:
        lines.append(" ".join(current))
    return lines or [text]


def _draw_nemotron_line(frame: np.ndarray, text: str, lay: dict) -> None:
    """Bottom strip so the vision-model read sits apart from the pose HUD."""
    fh, fw = frame.shape[:2]
    shown = "Nemotron: " + " ".join(text.split())
    font = lay["font"]
    max_w = fw - 16
    if " Situation: " in shown:
        head, body = shown.split(" Situation: ", 1)
        lines = [head, *_fit_hud_lines("Situation: " + body, max_w, font, lay["thick"], 2)]
    elif " Heard: " in shown:
        seen, heard = shown.split(" Heard: ", 1)
        lines = [seen, "Heard: " + heard]
    else:
        lines = [shown]
    bar_h = lay["hud_h"] * len(lines)
    y0 = max(0, fh - bar_h)
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, y0), (fw, fh), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
    font = lay["font"]
    for line in lines:
        while font > 0.2:
            (width, _height), _ = cv2.getTextSize(
                line, cv2.FONT_HERSHEY_SIMPLEX, font, lay["thick"]
            )
            if width <= fw - 16:
                break
            font = max(0.2, font * 0.9)
    for index, line in enumerate(lines):
        draw = line
        (width, _height), _ = cv2.getTextSize(
            draw, cv2.FONT_HERSHEY_SIMPLEX, font, lay["thick"]
        )
        while width > fw - 16 and len(draw) > 16:
            draw = draw[:-4] + "..."
            (width, _height), _ = cv2.getTextSize(
                draw, cv2.FONT_HERSHEY_SIMPLEX, font, lay["thick"]
            )
        baseline = y0 + int((index + 0.72) * lay["hud_h"])
        if baseline > fh - 2:
            baseline = fh - 2
        cv2.putText(
            frame,
            draw,
            (8, baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            font,
            (240, 240, 240),
            lay["thick"],
            cv2.LINE_AA,
        )


def to_display(frame: np.ndarray, scale: float) -> np.ndarray:
    if abs(scale - 1.0) < 0.02:
        return frame
    fh, fw = frame.shape[:2]
    return cv2.resize(
        frame,
        (max(1, int(fw * scale)), max(1, int(fh * scale))),
        interpolation=cv2.INTER_LINEAR if scale > 1 else cv2.INTER_AREA,
    )


def fit_center(
    frame: np.ndarray,
    canvas_w: int,
    canvas_h: int,
    *,
    bg: tuple[int, int, int] = (18, 18, 18),
) -> np.ndarray:
    """
    Scale video to fit inside canvas (letterbox/pillarbox) and center it.
    Fixes OpenCV fullscreen leaving the picture stuck in the top-left.
    """
    canvas_w = max(160, int(canvas_w))
    canvas_h = max(120, int(canvas_h))
    fh, fw = frame.shape[:2]
    if fh < 1 or fw < 1:
        return np.full((canvas_h, canvas_w, 3), bg, dtype=np.uint8)

    scale = min(canvas_w / fw, canvas_h / fh)
    nw = max(1, int(round(fw * scale)))
    nh = max(1, int(round(fh * scale)))
    interp = cv2.INTER_LINEAR if scale >= 1.0 else cv2.INTER_AREA
    resized = cv2.resize(frame, (nw, nh), interpolation=interp)

    canvas = np.full((canvas_h, canvas_w, 3), bg, dtype=np.uint8)
    x0 = (canvas_w - nw) // 2
    y0 = (canvas_h - nh) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    return canvas


def window_canvas_size(
    win: str,
    fallback_w: int,
    fallback_h: int,
    img_w: int,
    img_h: int,
) -> tuple[int, int]:
    """Use live window size when resized/fullscreen; else fallback canvas."""
    try:
        # (x, y, w, h) — available on most OpenCV builds
        _x, _y, ww, wh = cv2.getWindowImageRect(win)
        if ww >= 200 and wh >= 150:
            return int(ww), int(wh)
    except Exception:
        pass
    # Prefer at least the image size so we don't shrink needlessly
    return max(fallback_w, img_w), max(fallback_h, img_h)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        default=str(ROOT / "trackers" / "bytetrack_stable.yaml"),
        help="tracker yaml (default: longer-buffer ByteTrack for stable IDs)",
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
    ap.add_argument(
        "--audio",
        action="store_true",
        help="play the source sound track via ffplay (live RTSP or a video file)",
    )
    ap.add_argument(
        "--view-width",
        type=int,
        default=int(os.environ.get("POSE_VIEW_WIDTH", "0")),
        help="display width in px (0=auto from source size)",
    )
    ap.add_argument(
        "--view-scale",
        type=float,
        default=float(os.environ.get("POSE_VIEW_SCALE", "0")),
        help="display scale factor (0=auto; e.g. 2.0 doubles Sense)",
    )
    ap.add_argument(
        "--canvas",
        default=os.environ.get("POSE_CANVAS", "1280x720"),
        help="display canvas WxH; video is centered (fullscreen-friendly). 0x0=off",
    )
    ap.add_argument(
        "--nemotron",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("POSE_NEMOTRON", "0") == "1",
        help=(
            "ask Nemotron to read the room when a fall is suspected, "
            "and when you press n (NEMOTRON_ENDPOINT / NEMOTRON_MODEL)"
        ),
    )
    return ap.parse_args(argv)


def parse_canvas(spec: str) -> tuple[int, int]:
    """'1280x720' → (1280, 720); '0x0' or empty → (0, 0) disables letterbox."""
    raw = (spec or "").strip().lower()
    if not raw or raw in ("0", "0x0", "off", "none"):
        return 0, 0
    if "x" not in raw:
        return 1280, 720
    a, b = raw.split("x", 1)
    try:
        return max(0, int(a)), max(0, int(b))
    except ValueError:
        return 1280, 720


def resolve_source(raw: str):
    """Webcam index, URL, or file path. A pasted path may include odd spaces."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() else raw


def audio_playback_command(ffplay: str, source: str) -> list[str]:
    """Build an audio-only ffplay command. RTSP is forced over TCP."""
    command = [
        ffplay,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nodisp",
        "-vn",
        "-autoexit",
    ]
    if is_rtsp_url(source):
        command[1:1] = ["-rtsp_transport", "tcp"]
    else:
        command[1:1] = ["-probesize", "32", "-analyzeduration", "0"]
    command.append(source)
    return command


def start_rtsp_audio(source: str, *, enabled: bool = False) -> subprocess.Popen | None:
    """Play a live stream or video file's sound while OpenCV shows the picture."""
    if not enabled or not (is_rtsp_url(source) or is_video_file(source)):
        return None
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        print("audio: ffplay not found; install FFmpeg to use --audio", flush=True)
        return None
    try:
        player = subprocess.Popen(
            audio_playback_command(ffplay, source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"audio: could not start ffplay ({exc})", flush=True)
        return None
    kind = "RTSP" if is_rtsp_url(source) else "file"
    print(f"audio: playing {kind} audio via {ffplay}", flush=True)
    return player


def stop_audio_player(player: subprocess.Popen | None) -> None:
    """Stop the child player without delaying shutdown if it does not exit."""
    if player is None or player.poll() is not None:
        return
    player.terminate()
    try:
        player.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        player.kill()
        player.wait(timeout=1.0)


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
    if (
        isinstance(source, str)
        and Path(source).suffix.lower() in VIDEO_SUFFIXES
        and not Path(source).is_file()
    ):
        raise SystemExit(f"video file not found: {source}")
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
    tracker_last_seen: dict[int, float] = {}
    stable_ids = StablePersonIds(max_age=90)
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

    # Frame pullers for ESP MJPEG / MediaMTX RTSP (Ultralytics stream often stalls).
    # A local video file is a separate loop and does not replace those sources.
    mode = source_mode(source)
    mjpeg_mode = mode == "mjpeg"
    rtsp_mode = mode == "rtsp"
    frame_mode = mode != "live"
    audio_player = None
    audio_exit_reported = False
    audio_retry_at: float | None = None
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

    scene_asker = None
    if args.nemotron:
        from nemotron_scene import SceneAsker, endpoint_open

        think = os.environ.get("NEMOTRON_THINK", "0") == "1"

        scene_asker = SceneAsker(
            think=think,
            source=source if isinstance(source, str) else None,
        )
        print(
            f"nemotron: {scene_asker.endpoint} model={scene_asker.model} "
            f"think={think} (new fall, or press n; source audio when the "
            f"stream or file has a sound track)",
            flush=True,
        )
        if not endpoint_open(scene_asker.endpoint):
            print(
                "nemotron: tunnel is not open, so fall reads will fail until it is. "
                "Pose labels still run. Leave this SSH session up:\n"
                "  ssh -N -L 18554:10.128.93.13:8554 "
                "-L 18000:10.43.41.152:8000 ojl1977@10.128.81.10",
                flush=True,
            )

    win = "Patient pose + activity (q quit, n ask)" if args.nemotron else "Patient pose + activity (q quit)"
    canvas_w, canvas_h = parse_canvas(args.canvas)
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    if canvas_w > 0 and canvas_h > 0:
        cv2.resizeWindow(win, canvas_w, canvas_h)
    t0 = time.time()
    n = 0

    def handle_result(r, capture_timestamp: float | None = None) -> bool:
        """Annotate one YOLO result; return False if user quit."""
        nonlocal n, audio_exit_reported, audio_player, audio_retry_at
        n += 1
        if is_rtsp_url(source) and (
            audio_player is not None
            and audio_player.poll() is not None
            and not audio_exit_reported
        ):
            print("audio: ffplay exited; retrying shortly while video continues", flush=True)
            audio_player = None
            audio_retry_at = time.monotonic() + 3.0
            audio_exit_reported = True
        if (
            is_rtsp_url(source)
            and audio_player is None
            and audio_retry_at is not None
            and time.monotonic() >= audio_retry_at
        ):
            audio_player = start_rtsp_audio(source, enabled=args.audio)
            audio_retry_at = None
            audio_exit_reported = False
        frame = r.orig_img.copy()
        people: list[PersonView] = []
        fh, fw = frame.shape[:2]
        lay = view_layout(
            fw,
            fh,
            view_width=args.view_width,
            view_scale=args.view_scale,
        )
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
            display_ids = stable_ids.update(
                boxes[keep],
                [int(ids[i]) if ids is not None else None for i in keep],
            )
            if keep:
                areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
                for i, tid in zip(keep, display_ids):
                    if tid not in trackers:
                        trackers[tid] = ActivityTracker()
                    tracker_last_seen[tid] = time.monotonic()
                    box = tuple(float(x) for x in boxes[i])
                    st = trackers[tid].update(
                        kdata[i],
                        box=box,
                        frame_hw=(fh, fw),
                        timestamp=capture_timestamp,
                    )
                    people.append(
                        PersonView(
                            track_id=tid,
                            activity=st,
                            box=box,
                            area=float(areas[i]),
                        )
                    )
                    color = ACTIVITY_COLOR.get(st.label, (120, 120, 120))
                    draw_person(frame, boxes[i], kdata[i], color)
                    x1, y1, x2, y2 = map(int, boxes[i])
                    tag = f"ID{tid} | {st.label.replace('_', ' ')}"
                    font_sc = lay["tag_font"]
                    thick = lay["thick"]
                    (tw, th), _ = cv2.getTextSize(
                        tag, cv2.FONT_HERSHEY_SIMPLEX, font_sc, thick
                    )
                    pad = max(2, fh // 160)
                    hud_h = lay["hud_h"]
                    ty1 = max(hud_h + 2, min(y1 + 2, max(0, y2 - th - 2 * pad)))
                    ty2 = ty1 + th + 2 * pad
                    tx2 = min(fw - 1, x1 + tw + 2 * pad)
                    cv2.rectangle(frame, (x1, ty1), (tx2, ty2), color, -1)
                    cv2.putText(
                        frame,
                        tag,
                        (x1 + pad, ty2 - pad - 1),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        font_sc,
                        (255, 255, 255),
                        thick,
                        cv2.LINE_AA,
                    )
        else:
            stable_ids.update(np.empty((0, 4), dtype=float), [])

        expired_trackers = [
            tid
            for tid, last_seen in tracker_last_seen.items()
            if time.monotonic() - last_seen > 15.0
        ]
        for tid in expired_trackers:
            tracker_last_seen.pop(tid, None)
            trackers.pop(tid, None)

        scene = summarize(people)
        counts = {lab: 0 for lab in ALL_ACTIVITIES}
        for person in people:
            counts[person.activity.label] = counts.get(person.activity.label, 0) + 1
        fps = n / max(1e-3, time.time() - t0)
        hud_h = lay["hud_h"]
        hud = frame.copy()
        cv2.rectangle(hud, (0, 0), (fw, hud_h), (15, 15, 15), -1)
        cv2.addWeighted(hud, 0.55, frame, 0.45, 0, frame)
        activity_text = ", ".join(
            f"{lab.replace('_', ' ').title()}: {count}"
            for lab, count in counts.items()
            if count and lab != "fall_suspected"
        )
        if not activity_text:
            activity_text = (
                "Fall suspected"
                if counts["fall_suspected"]
                else "No activity detected"
            )
        status = (
            f"People: {scene.people_count} | {activity_text} | "
            f"Fall alerts: {scene.fall_alerts} | FPS: {fps:.0f}"
        )
        status_font = lay["font"]
        while status_font > 0.2:
            (status_width, _status_height), _ = cv2.getTextSize(
                status, cv2.FONT_HERSHEY_SIMPLEX, status_font, lay["thick"]
            )
            if status_width <= fw - 16:
                break
            status_font = max(0.2, status_font * 0.9)
        cv2.putText(
            frame,
            status,
            (8, lay["status_y"]),
            cv2.FONT_HERSHEY_SIMPLEX,
            status_font,
            (240, 240, 240),
            lay["thick"],
            cv2.LINE_AA,
        )
        if scene_asker is not None:
            # Every frame, including an empty list, so a recovered track can re-arm.
            scene_asker.on_falls(
                r.orig_img,
                [
                    person.track_id
                    for person in people
                    if person.activity.label == "fall_suspected"
                ],
            )
        if scene.fall_alerts:
            cv2.putText(
                frame,
                "FALL SUSPECTED",
                (8, hud_h + max(18, fh // 20)),
                cv2.FONT_HERSHEY_SIMPLEX,
                max(lay["font"], 0.55),
                (0, 0, 255),
                max(lay["thick"], 2),
                cv2.LINE_AA,
            )
        nemotron_line = scene_asker.status_line() if scene_asker is not None else ""
        if nemotron_line:
            _draw_nemotron_line(frame, nemotron_line, lay)
        view = to_display(frame, lay["scale"])
        if canvas_w > 0 and canvas_h > 0:
            cw, ch = window_canvas_size(
                win, canvas_w, canvas_h, view.shape[1], view.shape[0]
            )
            view = fit_center(view, cw, ch)
        cv2.imshow(win, view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            return False
        if key == ord("n") and scene_asker is not None:
            if scene_asker.ask_now(r.orig_img):
                print("nemotron: asking about this frame", flush=True)
            else:
                print("nemotron: busy", flush=True)
        if n % 30 == 0:
            acts = {lab: counts[lab] for lab in ALL_ACTIVITIES if counts[lab]}
            print(
                f"people={scene.people_count} acts={acts} "
                f"falls={scene.fall_alerts} fps={fps:.1f} "
                f"src={fw}x{fh} view={view.shape[1]}x{view.shape[0]} "
                f"scale={lay['scale']:.2f}",
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

    audio_started: float | None = None

    def restart_file_audio() -> None:
        nonlocal audio_player, audio_started
        if not is_video_file(source):
            return
        stop_audio_player(audio_player)
        audio_player = start_rtsp_audio(str(source), enabled=args.audio)
        audio_started = time.monotonic() if audio_player is not None else None

    def file_audio_clock() -> float | None:
        """Seconds of file audio already heard. ffplay needs a short startup."""
        if audio_started is None:
            return None
        return max(0.0, time.monotonic() - audio_started - 0.12)

    audio_player = None if is_video_file(source) else start_rtsp_audio(source, enabled=args.audio)
    try:
        if frame_mode:
            if mjpeg_mode:
                frames = iter_mjpeg(str(source))
                print("opening stream (first frame can take a few seconds)…", flush=True)
            elif rtsp_mode:
                frames = iter_rtsp(str(source))
                print("opening stream (first frame can take a few seconds)…", flush=True)
            else:
                frames = iter_video_loop(
                    str(source),
                    on_media_time=scene_asker.note_time if scene_asker is not None else None,
                    on_loop=restart_file_audio if args.audio else None,
                    sync_to=file_audio_clock if args.audio else None,
                )
                print("video file loops until q; Nemotron still uses n or a new fall", flush=True)
            for frame, capture_timestamp in frames:
                out = _infer_frame(frame)
                if not out:
                    continue
                if not handle_result(out[0], capture_timestamp):
                    break
        else:
            assert results_iter is not None
            for r in results_iter:
                if not handle_result(r, time.monotonic()):
                    break
    finally:
        if scene_asker is not None:
            scene_asker.close()
        stop_audio_player(audio_player)
        cv2.destroyAllWindows()
        print(f"stopped after {n} frames", flush=True)


if __name__ == "__main__":
    main()
