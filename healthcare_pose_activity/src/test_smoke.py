#!/usr/bin/env python3
"""
Vigorous offline smoke tests (no camera / no live RTSP required).
Run:  uv run --with numpy --with opencv-python python src/test_smoke.py
Optional YOLO:  RUN_YOLO=1 uv run --with "ultralytics>=8.4.175" ... python src/test_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from activity import ALL_ACTIVITIES, ActivityTracker, classify_frame  # noqa: E402
from filters import draw_person, filter_person_indices  # noqa: E402
from live_infer import (  # noqa: E402
    ACTIVITY_COLOR,
    audio_playback_command,
    fit_center,
    parse_args,
    start_rtsp_audio,
    to_display,
    view_layout,
)
from mjpeg import is_mjpeg_url, is_rtsp_url  # noqa: E402
from patients import PersonView, summarize  # noqa: E402
from test_activity import (  # noqa: E402
    lying_kpts,
    sitting_kpts,
    standing_kpts,
    test_all_labels_covered,
    test_classify,
    test_activity_hysteresis_on_pose_dropout,
    test_fall,
    test_fall_rejects_posture_only_and_gaps,
    test_posture_transition,
    test_scene,
    test_stable_person_ids,
    test_unknown_when_pose_is_insufficient,
    test_walking,
)


def test_url_detect() -> None:
    assert is_mjpeg_url("http://10.128.93.17:81/stream")
    assert is_mjpeg_url("http://10.128.93.17:81/mjpeg")
    assert not is_mjpeg_url("rtsp://127.0.0.1:8554/cam_sense")
    assert not is_mjpeg_url("0")
    assert is_rtsp_url("rtsp://127.0.0.1:18554/cam_sense")
    assert not is_rtsp_url("http://x:81/stream")
    print("url_detect OK")


def test_rtsp_audio_command() -> None:
    command = audio_playback_command(
        "/opt/homebrew/bin/ffplay", "rtsp://127.0.0.1:18554/cam_sense"
    )
    assert command[0] == "/opt/homebrew/bin/ffplay"
    assert "-rtsp_transport" in command and "tcp" in command
    assert "-nodisp" in command and "-vn" in command
    assert command[-1] == "rtsp://127.0.0.1:18554/cam_sense"
    assert parse_args(["rtsp://127.0.0.1:18554/cam_sense"]).audio is False
    assert parse_args(["rtsp://127.0.0.1:18554/cam_sense", "--audio"]).audio is True
    with patch("live_infer.shutil.which", return_value="/fake/ffplay"), patch(
        "live_infer.subprocess.Popen"
    ) as popen:
        popen.return_value = object()
        assert start_rtsp_audio(
            "rtsp://127.0.0.1:18554/cam_sense", enabled=False
        ) is None
        player = start_rtsp_audio(
            "rtsp://127.0.0.1:18554/cam_sense", enabled=True
        )
        assert player is popen.return_value
        assert popen.call_args.args[0][-1] == "rtsp://127.0.0.1:18554/cam_sense"
        popen.assert_called_once()
    print("RTSP audio playback command OK")


def test_view_layout_matrix() -> None:
    cases = [
        (320, 240),
        (480, 320),  # Sense
        (640, 480),
        (1280, 720),
        (1920, 1080),
        (2560, 1440),
    ]
    for fw, fh in cases:
        lay = view_layout(fw, fh)
        assert 20 <= lay["hud_h"] <= 52, lay
        assert lay["hud_h"] / fh <= 0.12, f"HUD too tall for {fw}x{fh}: {lay}"
        assert lay["scale"] > 0
        out_w = int(fw * lay["scale"])
        # tiny sources get upscaled; huge get shrunk or stay
        if fw < 640:
            assert out_w >= 900, (fw, out_w, lay)
        if fw == 1280:
            assert abs(lay["scale"] - 1.0) < 0.05

    # overrides
    lay_w = view_layout(480, 320, view_width=1280)
    assert abs(lay_w["scale"] - 1280 / 480) < 0.01
    lay_s = view_layout(480, 320, view_scale=3.0)
    assert abs(lay_s["scale"] - 3.0) < 0.01
    print("view_layout OK:", [(f"{w}x{h}", round(view_layout(w, h)["scale"], 2)) for w, h in cases])


def test_to_display() -> None:
    img = np.zeros((320, 480, 3), dtype=np.uint8)
    out = to_display(img, 2.0)
    assert out.shape[1] == 960 and out.shape[0] == 640
    same = to_display(img, 1.0)
    assert same.shape == img.shape
    print("to_display OK")


def test_fit_center() -> None:
    img = np.full((320, 480, 3), 80, dtype=np.uint8)
    canvas = fit_center(img, 1280, 720)
    assert canvas.shape == (720, 1280, 3)
    # video should be centered — middle of canvas not background-only
    mid = canvas[360, 640]
    assert int(mid.sum()) > 0
    # corners stay near background
    assert int(canvas[0, 0].sum()) < 80
    # widescreen canvas with tall content → pillarbox left/right
    tall = np.full((640, 360, 3), 90, dtype=np.uint8)
    c2 = fit_center(tall, 1280, 720)
    assert c2.shape == (720, 1280, 3)
    print("fit_center OK: 480x320 → centered in 1280x720")


def test_filters_and_draw() -> None:
    fh, fw = 480, 640
    # real-ish person box + tiny hand false positive
    k_person = standing_kpts()
    k_hand = np.zeros((17, 3), dtype=float)
    k_hand[9] = [100, 100, 0.9]  # wrist only
    k_hand[10] = [120, 110, 0.9]
    boxes = np.array(
        [
            [180, 80, 300, 500],  # person
            [90, 90, 140, 130],  # hand-like
        ],
        dtype=float,
    )
    kdata = np.stack([k_person, k_hand], axis=0)
    keep = filter_person_indices(kdata, boxes, (fh, fw))
    assert 0 in keep, keep
    assert 1 not in keep, f"hand should be filtered: {keep}"

    # A legitimate distant person should survive even if their box is much
    # smaller than the foreground person's box.
    two_people = np.array(
        [
            [180, 40, 500, 450],
            [20, 20, 90, 140],
            [520, 20, 570, 110],
        ],
        dtype=float,
    )
    valid_poses = np.stack([k_person, k_person, k_person], axis=0)
    keep_two = filter_person_indices(valid_poses, two_people, (fh, fw))
    assert keep_two == [0, 1, 2], keep_two

    frame = np.zeros((fh, fw, 3), dtype=np.uint8)
    draw_person(frame, boxes[0], k_person, ACTIVITY_COLOR["standing"])
    assert frame.sum() > 0
    print("filters_and_draw OK:", keep, "multi-scale:", keep_two)


def test_overlay_pipeline_all_sizes() -> None:
    """Simulate HUD+tag draw path on multiple resolutions (headless)."""
    sizes = [(480, 320), (640, 480), (1280, 720)]
    for fw, fh in sizes:
        frame = np.full((fh, fw, 3), 40, dtype=np.uint8)
        lay = view_layout(fw, fh)
        k = standing_kpts()
        # scale keypoints roughly into frame
        k = k.copy()
        k[:, 0] *= fw / 640.0
        k[:, 1] *= fh / 720.0
        box = (fw * 0.25, fh * 0.1, fw * 0.55, fh * 0.9)
        t = ActivityTracker()
        st = t.update(k, box=box, frame_hw=(fh, fw))
        color = ACTIVITY_COLOR.get(st.label, (120, 120, 120))
        draw_person(frame, np.array(box), k, color)
        hud = frame.copy()
        cv2.rectangle(hud, (0, 0), (fw, lay["hud_h"]), (15, 15, 15), -1)
        cv2.addWeighted(hud, 0.55, frame, 0.45, 0, frame)
        cv2.putText(
            frame,
            f"P:1 sit test {st.label}",
            (8, lay["status_y"]),
            cv2.FONT_HERSHEY_SIMPLEX,
            lay["font"],
            (240, 240, 240),
            lay["thick"],
            cv2.LINE_AA,
        )
        view = to_display(frame, lay["scale"])
        assert view.shape[0] > 0 and view.shape[1] >= fw * 0.9
        # write one sample for visual check
        if (fw, fh) == (480, 320):
            out = ROOT / "tmp_smoke_sense_overlay.png"
            cv2.imwrite(str(out), view)
            assert out.exists() and out.stat().st_size > 1000
            print(f"  wrote {out.name} {view.shape[1]}x{view.shape[0]}")
    print("overlay_pipeline OK")


def test_activity_tracker_burst() -> None:
    """Many frames sit/stand/walk/lie without crash."""
    t = ActivityTracker()
    labels = set()
    for i in range(30):
        st = t.update(
            standing_kpts(dx=i * 20.0),
            box=(180 + i * 20, 80, 300 + i * 20, 500),
            frame_hw=(720, 1280),
        )
        labels.add(st.label)
    assert "walking" in labels or "standing" in labels, labels
    for _ in range(10):
        st = t.update(sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280))
    assert st.label in ("sitting", "standing", "walking"), st
    for _ in range(5):
        st = t.update(lying_kpts(), box=(50, 300, 500, 420), frame_hw=(720, 1280))
    assert st.label in ("lying", "fall_suspected"), st
    print("activity_burst OK:", sorted(labels), "last=", st.label)


def test_yolo_offline_video() -> None:
    if os.environ.get("RUN_YOLO", "0") != "1":
        print("yolo SKIP (RUN_YOLO=0)")
        return
    print("loading YOLO (may download weights)…", flush=True)
    from ultralytics import YOLO

    # Prefer nano for smoke speed; fall back to whatever is default quality
    model_name = os.environ.get("SMOKE_POSE_MODEL", "yolo26n-pose.pt")
    model = YOLO(model_name)
    assert getattr(model, "task", None) == "pose"

    # Synthetic clip: moving blob (not a real person — just pipeline integrity)
    tmp = Path(tempfile.mkdtemp(prefix="pose_smoke_"))
    vid = tmp / "synth.mp4"
    wh = (640, 480)
    writer = cv2.VideoWriter(
        str(vid), cv2.VideoWriter_fourcc(*"mp4v"), 8, wh
    )
    for i in range(16):
        fr = np.full((wh[1], wh[0], 3), 30, dtype=np.uint8)
        cv2.rectangle(
            fr,
            (80 + i * 8, 60),
            (220 + i * 8, 400),
            (200, 180, 160),
            -1,
        )
        writer.write(fr)
    writer.release()

    n = 0
    # Headless: no show; stream from file
    for r in model.predict(
        source=str(vid),
        stream=True,
        conf=0.25,
        imgsz=640,
        device="cpu",
        verbose=False,
        classes=[0],
        max_det=10,
    ):
        n += 1
        # exercise post-path even if 0 detections
        frame = r.orig_img.copy()
        fh, fw = frame.shape[:2]
        lay = view_layout(fw, fh)
        if r.boxes is not None and len(r.boxes) and r.keypoints is not None:
            kdata = r.keypoints.data.cpu().numpy()
            boxes = r.boxes.xyxy.cpu().numpy()
            keep = filter_person_indices(kdata, boxes, (fh, fw))
            for i in keep:
                draw_person(frame, boxes[i], kdata[i], (40, 180, 40))
        view = to_display(frame, lay["scale"])
        assert view.size > 0
    assert n >= 8, n
    print(f"yolo_offline OK: frames={n} model={model_name}")


def test_colors_cover_activities() -> None:
    assert set(ALL_ACTIVITIES) <= set(ACTIVITY_COLOR)
    print("colors OK")


def main() -> None:
    print("=== vigorous offline smoke ===", flush=True)
    test_all_labels_covered()
    test_classify()
    test_posture_transition()
    test_unknown_when_pose_is_insufficient()
    test_walking()
    test_fall()
    test_fall_rejects_posture_only_and_gaps()
    test_stable_person_ids()
    test_scene()
    test_url_detect()
    test_rtsp_audio_command()
    test_view_layout_matrix()
    test_to_display()
    test_fit_center()
    test_filters_and_draw()
    test_overlay_pipeline_all_sizes()
    test_activity_tracker_burst()
    test_activity_hysteresis_on_pose_dropout()
    test_colors_cover_activities()
    test_yolo_offline_video()
    print("=== ALL_SMOKE_OK ===", flush=True)


if __name__ == "__main__":
    main()
