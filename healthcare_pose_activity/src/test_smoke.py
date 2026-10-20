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
from mjpeg import is_mjpeg_url, is_rtsp_url, is_video_file, iter_video_loop, source_mode  # noqa: E402
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
    assert not is_video_file("rtsp://127.0.0.1:18554/cam_sense")
    assert not is_video_file("0")
    assert not is_video_file("http://10.128.93.17:81/stream")
    from live_infer import resolve_source

    assert resolve_source("\u00a0\u00a0/tmp/clip.mp4") == "/tmp/clip.mp4"
    assert resolve_source("  0") == 0
    assert source_mode("rtsp://127.0.0.1:18554/cam_sense") == "rtsp"
    assert source_mode("http://10.128.93.17:81/stream") == "mjpeg"
    assert source_mode(0) == "live"
    assert source_mode("0") == "live"
    print("url_detect OK")


def test_video_file_loops() -> None:
    """A short file plays past the end and returns to the first frame."""
    folder = Path(tempfile.mkdtemp(prefix="pose_loop_"))
    path = folder / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (32, 24))
    marks = (20, 80, 140, 200)
    for value in marks:
        frame = np.full((24, 32, 3), value, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    assert is_video_file(str(path)), path
    assert source_mode(str(path)) == "video"
    assert source_mode("rtsp://127.0.0.1:8554/cam_sense") == "rtsp"

    seen: list[int] = []
    for index, (frame, _ts) in enumerate(iter_video_loop(str(path), pace=False)):
        seen.append(int(frame[0, 0, 0]))
        if index >= 6:
            break
    assert seen[:4] == list(marks), seen
    assert seen[4] == marks[0] and seen[5] == marks[1], seen

    # Sound is ahead: frames already behind that clock are not shown.
    dropped: list[int] = []
    for _index, (frame, _ts) in enumerate(
        iter_video_loop(str(path), pace=False, sync_to=lambda: 0.25)
    ):
        dropped.append(int(frame[0, 0, 0]))
        if len(dropped) >= 2:
            break
    assert dropped[0] == marks[2], dropped
    print("video loop OK", seen)


def test_rtsp_audio_command() -> None:
    command = audio_playback_command(
        "/opt/homebrew/bin/ffplay", "rtsp://127.0.0.1:18554/cam_sense"
    )
    assert command[0] == "/opt/homebrew/bin/ffplay"
    assert "-rtsp_transport" in command and "tcp" in command
    assert "-nodisp" in command and "-vn" in command
    assert command[-1] == "rtsp://127.0.0.1:18554/cam_sense"
    file_command = audio_playback_command("/opt/homebrew/bin/ffplay", "/tmp/clip.mp4")
    assert "-rtsp_transport" not in file_command
    assert file_command[-1] == "/tmp/clip.mp4"
    assert "-nodisp" in file_command and "-vn" in file_command
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
        with tempfile.NamedTemporaryFile(suffix=".mp4") as clip:
            played = start_rtsp_audio(clip.name, enabled=True)
        assert played is popen.return_value
        assert popen.call_args.args[0][-1].endswith(".mp4")
        assert "-rtsp_transport" not in popen.call_args.args[0]
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
        assert 22 <= lay["hud_h"] <= 60, lay
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

    # Two boxes on one standing person, plus a separate person, count as two.
    duplicate = np.array(
        [
            [200, 40, 340, 520],
            [215, 60, 330, 480],
            [500, 80, 640, 520],
        ],
        dtype=float,
    )
    duplicate_poses = np.stack([k_person, k_person, k_person], axis=0)
    keep_deduped = filter_person_indices(duplicate_poses, duplicate, (720, 1280))
    assert keep_deduped == [0, 2], keep_deduped

    # People standing side by side still both count. Only a box on the same body is dropped.
    side_by_side = np.array(
        [
            [100, 50, 220, 460],
            [200, 50, 330, 460],
        ],
        dtype=float,
    )
    keep_side = filter_person_indices(
        np.stack([k_person, k_person], axis=0), side_by_side, (720, 1280)
    )
    assert keep_side == [0, 1], keep_side

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


def _nemotron_reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_nemotron_scene_on_fall() -> None:
    """Fall asks, dedup, re-arm, backoff, and a second person arriving mid-request."""
    import json
    import threading
    import time
    import urllib.error
    from unittest.mock import patch

    from nemotron_scene import SceneAsker, format_reply, frame_data_url

    frame = np.zeros((80, 120, 3), dtype=np.uint8)
    frame[10:70, 20:100] = (40, 80, 120)
    url = frame_data_url(frame, max_px=64)
    assert url.startswith("data:image/jpeg;base64,")
    raw = __import__("base64").b64decode(url.split(",", 1)[1])
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and max(decoded.shape[:2]) <= 64

    wide = np.zeros((1000, 2000, 3), dtype=np.uint8)
    wide_url = frame_data_url(wide, max_px=64)
    wide_raw = __import__("base64").b64decode(wide_url.split(",", 1)[1])
    wide_img = cv2.imdecode(np.frombuffer(wide_raw, np.uint8), cv2.IMREAD_COLOR)
    assert wide_img is not None and max(wide_img.shape[:2]) <= 64

    bed = json.dumps(
        {
            "people": 1,
            "postures": ["lying"],
            "fall_on_floor": False,
            "note": "Person is on a bed.",
        }
    )
    reply = _nemotron_reply(bed)
    assert format_reply(reply) == "1 person: lying; no floor fall. Person is on a bed."
    thought = _nemotron_reply(
        "<think>looks like a bed</think>"
        + json.dumps(
            {
                "people": 2,
                "postures": ["sitting", "standing"],
                "fall_on_floor": True,
                "note": "One is down.",
            }
        )
    )
    assert format_reply(thought) == "2 people: sitting, standing; fall on floor. One is down."
    heard = json.loads(bed)
    heard["heard"] = "A nurse asks how she feels."
    assert format_reply(_nemotron_reply(json.dumps(heard))).endswith(
        "Heard: A nurse asks how she feels."
    )
    situation = json.loads(bed)
    situation["situation"] = (
        "A patient is lying in bed while a nurse asks how she feels. The room is otherwise quiet."
    )
    assert format_reply(_nemotron_reply(json.dumps(situation))).endswith(
        "Situation: A patient is lying in bed while a nurse asks how she feels. The room is otherwise quiet."
    )
    assert format_reply(_nemotron_reply("not json")) == "not json"
    try:
        format_reply({"choices": []})
        raise AssertionError("missing message should fail")
    except ValueError:
        pass

    bodies: list[dict] = []

    def fake_post(endpoint, body, timeout):
        assert endpoint == "http://nemotron.test/v1"
        assert body["max_tokens"] == 256
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        assert "Do not diagnose" in body["messages"][0]["content"]
        assert body["response_format"]["json_schema"]["schema"]["required"] == [
            "people",
            "postures",
            "fall_on_floor",
            "note",
        ]
        assert "fall_suspected" in body["messages"][1]["content"][0]["text"]
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        bodies.append(body)
        return reply

    class Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = Clock()
    with patch("_nocommit.post", fake_post):
        asker = SceneAsker(
            endpoint="http://nemotron.test/v1",
            model="nemotron",
            timeout=5,
            rearm_after=1.0,
            retry_s=10.0,
            clock=clock,
        )
        asker.on_falls(frame, [1, 2])
        asker.wait(3)
        assert "bed" in asker.status_line(), asker.status_line()
        clock.t = 0.2
        asker.on_falls(frame, [1, 2])
        asker.wait(3)
        assert len(bodies) == 1, len(bodies)

        # A one-frame gap must not send the same fall again.
        clock.t = 0.4
        asker.on_falls(frame, [])
        clock.t = 0.5
        asker.on_falls(frame, [1])
        asker.wait(3)
        assert len(bodies) == 1, len(bodies)

        # After the fall has been gone long enough, the same track asks again.
        clock.t = 2.0
        asker.on_falls(frame, [])
        clock.t = 2.2
        asker.on_falls(frame, [1])
        asker.wait(3)
        assert len(bodies) == 2, len(bodies)

    # A second person who falls while a request is running is asked afterwards.
    release = threading.Event()
    seen: list[int] = []
    try:
        def blocking_post(endpoint, body, timeout):
            seen.append(1)
            if len(seen) == 1:
                release.wait(3)
            return reply

        with patch("_nocommit.post", blocking_post):
            asker = SceneAsker(
                endpoint="http://nemotron.test/v1",
                model="nemotron",
                timeout=5,
                clock=clock,
            )
            clock.t = 10
            asker.on_falls(frame, [3])
            for _ in range(100):
                if asker.status_line().endswith("…"):
                    break
                time.sleep(0.01)
            assert asker.status_line().endswith("…"), asker.status_line()
            assert asker.ask_now(frame) is False
            clock.t = 10.2
            asker.on_falls(frame, [3, 9])
            release.set()
            asker.wait(3)
            assert len(seen) == 1, len(seen)
            clock.t = 10.4
            asker.on_falls(frame, [3, 9])
            asker.wait(3)
            assert len(seen) == 2, len(seen)
            clock.t = 10.6
            asker.on_falls(frame, [3, 9])
            asker.wait(3)
            assert len(seen) == 2, len(seen)
    finally:
        release.set()

    # A failed fall waits out the backoff. A failed key press does not block falls.
    calls = {"n": 0}

    def flaky_post(endpoint, body, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("down")
        return reply

    with patch("_nocommit.post", flaky_post):
        clock.t = 100
        asker = SceneAsker(
            endpoint="http://nemotron.test/v1",
            model="nemotron",
            timeout=5,
            retry_s=10,
            clock=clock,
        )
        asker.on_falls(frame, [7])
        asker.wait(3)
        assert "down" in asker.status_line(), asker.status_line()
        assert "…" not in asker.status_line()
        clock.t = 105
        asker.on_falls(frame, [7])
        asker.wait(0.3)
        assert calls["n"] == 1, calls
        assert asker.ask_now(frame) is True
        asker.wait(3)
        assert calls["n"] == 2, calls
        assert "bed" in asker.status_line(), asker.status_line()
        clock.t = 106
        asker.on_falls(frame, [7])
        asker.wait(0.3)
        assert calls["n"] == 2, calls
        clock.t = 111
        asker.on_falls(frame, [7])
        asker.wait(3)
        assert calls["n"] == 3, calls

        calls["n"] = 0
        clock.t = 200
        asker = SceneAsker(
            endpoint="http://nemotron.test/v1",
            model="nemotron",
            timeout=5,
            retry_s=10,
            clock=clock,
        )
        assert asker.ask_now(frame) is True
        asker.wait(3)
        assert "down" in asker.status_line(), asker.status_line()
        clock.t = 200.2
        asker.on_falls(frame, [4])
        asker.wait(3)
        assert calls["n"] == 2, calls
        assert "bed" in asker.status_line(), asker.status_line()
    print("nemotron scene OK")


def test_nemotron_sends_source_audio() -> None:
    """A sound clip from the source is attached beside the frame. No clip stays picture-only."""
    import json
    from unittest.mock import patch

    from nemotron_scene import SceneAsker

    frame = np.zeros((40, 40, 3), dtype=np.uint8)
    bed = json.dumps(
        {
            "people": 1,
            "postures": ["standing"],
            "fall_on_floor": False,
            "note": "A door closed.",
        }
    )
    reply = _nemotron_reply(bed)
    bodies: list[dict] = []

    def fake_post(endpoint, body, timeout):
        bodies.append(body)
        return reply

    clip = "data:audio/flac;base64,AAAA"
    with patch("_nocommit.post", fake_post):
        asker = SceneAsker(
            endpoint="http://nemotron.test/v1",
            model="nemotron",
            timeout=5,
            audio_clip=lambda: clip,
        )
        assert asker.ask_now(frame) is True
        asker.wait(3)
        parts = bodies[-1]["messages"][1]["content"]
        assert parts[1]["type"] == "image_url"
        assert parts[2] == {"type": "audio_url", "audio_url": {"url": clip}}
        assert "previous 8 seconds" in parts[0]["text"]
        assert "situation is one or two short sentences" in parts[0]["text"]
        assert bodies[-1]["response_format"]["json_schema"]["schema"]["required"] == [
            "people",
            "postures",
            "fall_on_floor",
            "situation",
        ]
        assert bodies[-1]["max_tokens"] == 480

        quiet = SceneAsker(
            endpoint="http://nemotron.test/v1",
            model="nemotron",
            timeout=5,
            audio_clip=lambda: None,
        )
        assert quiet.ask_now(frame) is True
        quiet.wait(3)
        quiet_parts = bodies[-1]["messages"][1]["content"]
        assert len(quiet_parts) == 2
        assert quiet_parts[1]["type"] == "image_url"
    import shutil
    import struct
    import tempfile
    from pathlib import Path

    from source_audio import SourceAudio, _file_clip, has_audio_track

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        rate, n = 16000, 16000
        pcm = b"\x00\x02" * n
        wav_bytes = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + len(pcm),
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            rate,
            rate * 2,
            2,
            16,
            b"data",
            len(pcm),
        ) + pcm
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "tone.wav"
            wav.write_bytes(wav_bytes)
            assert has_audio_track(str(wav))
            payload = _file_clip(str(wav), 0.8)
            assert payload and payload.startswith(b"fLaC"), payload[:4] if payload else None
            silent = Path(tmp) / "silent.avi"
            writer = cv2.VideoWriter(
                str(silent), cv2.VideoWriter_fourcc(*"MJPG"), 8, (16, 16)
            )
            assert writer.isOpened()
            for _ in range(4):
                writer.write(np.zeros((16, 16, 3), dtype=np.uint8))
            writer.release()
            tapped = SourceAudio(str(silent))
            if tapped._thread is not None:
                tapped._thread.join(8)
            assert tapped.data_url() is None
            tapped.close()
    print("nemotron source audio OK")


def test_nemotron_hud_and_flag() -> None:
    from live_infer import _draw_nemotron_line, parse_args, view_layout

    frame = np.full((320, 480, 3), 200, dtype=np.uint8)
    lay = view_layout(480, 320)
    _draw_nemotron_line(frame, "1 person: lying; no floor fall. Person is on a bed.", lay)
    assert int(frame[10, 10].sum()) == 600
    bar_h = lay["hud_h"]
    bar = frame[-bar_h:, :]
    bright = np.any(bar > 200, axis=2)
    assert int(bright.sum()) > 20, int(bright.sum())
    rows = np.where(bright.any(axis=1))[0]
    assert int(rows.min()) < bar_h - 2
    assert int(rows.max()) <= bar_h - 1
    long = np.full((240, 320, 3), 180, dtype=np.uint8)
    _draw_nemotron_line(long, "word " * 80, view_layout(320, 240))
    situation_frame = np.full((320, 480, 3), 200, dtype=np.uint8)
    _draw_nemotron_line(
        situation_frame,
        "1 person: lying; no floor fall. Situation: A patient is lying in bed while a nurse asks how she feels.",
        lay,
    )
    situation_top = 320 - lay["hud_h"] * 2
    assert int(situation_frame[situation_top + 2, 10].sum()) < 600
    out = ROOT / "tmp_smoke_nemotron_hud.png"
    cv2.imwrite(str(out), frame)
    assert out.exists() and out.stat().st_size > 1000

    with patch.dict(os.environ, {"POSE_NEMOTRON": "0"}):
        assert parse_args(["0"]).nemotron is False
        assert parse_args(["0", "--nemotron"]).nemotron is True
    with patch.dict(os.environ, {"POSE_NEMOTRON": "1"}):
        assert parse_args(["rtsp://127.0.0.1/cam"]).nemotron is True
        assert parse_args(["0", "--no-nemotron"]).nemotron is False
    from nemotron_scene import endpoint_open

    class _OpenSock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    with patch("socket.create_connection", side_effect=OSError("refused")):
        assert endpoint_open("http://127.0.0.1:18000/v1", timeout=0.2) is False
    with patch("socket.create_connection", return_value=_OpenSock()) as opened:
        assert endpoint_open("http://127.0.0.1:18000/v1", timeout=0.2) is True
        opened.assert_called_once_with(("127.0.0.1", 18000), timeout=0.2)
    print("nemotron hud OK", f"bar_h={bar_h}", f"text_rows={int(rows.min())}-{int(rows.max())}")


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
    test_video_file_loops()
    test_rtsp_audio_command()
    test_view_layout_matrix()
    test_to_display()
    test_fit_center()
    test_filters_and_draw()
    test_overlay_pipeline_all_sizes()
    test_activity_tracker_burst()
    test_activity_hysteresis_on_pose_dropout()
    test_nemotron_scene_on_fall()
    test_nemotron_sends_source_audio()
    test_nemotron_hud_and_flag()
    test_colors_cover_activities()
    test_yolo_offline_video()
    print("=== ALL_SMOKE_OK ===", flush=True)


if __name__ == "__main__":
    main()
