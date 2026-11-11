#!/usr/bin/env python3
"""RealSense D456 → MediaPipe pose → depth-lifted 3D joints (camera meters).

Library module for wifi_pose_3d GT recording. Not the old live-only viewer.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODEL = ROOT / "models" / "pose_landmarker_lite.task"
OUT_JSON = ROOT / "sessions" / "_latest_joints.json"
LIB_PATH = "/opt/homebrew/lib/librealsense2.dylib"
RS2_API_VERSION = 2 * 10000 + 59 * 100 + 1  # 2.59.1

RS2_STREAM_DEPTH = 1
RS2_STREAM_COLOR = 2
RS2_FORMAT_Z16 = 1
RS2_FORMAT_BGR8 = 6

# Prefer modes the D456 actually lists; first match wins.
STREAM_MODES = (
    (640, 480, 30),
    (848, 480, 30),
    (1280, 720, 30),
)

# MediaPipe pose, 33 landmarks.
NAMES = [
    "nose",
    "left_eye_inner",
    "left_eye",
    "left_eye_outer",
    "right_eye_inner",
    "right_eye",
    "right_eye_outer",
    "left_ear",
    "right_ear",
    "mouth_left",
    "mouth_right",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_pinky",
    "right_pinky",
    "left_index",
    "right_index",
    "left_thumb",
    "right_thumb",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
]
BONES = [
    (0, 1), (1, 2), (2, 3), (3, 7),
    (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10),
    (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21), (17, 19),
    (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (27, 31), (29, 31),
    (24, 26), (26, 28), (28, 30), (28, 32), (30, 32),
]
FOOT_IDS = (27, 28, 29, 30, 31, 32)
# Need a real torso before treating MediaPipe hits as a person.
TORSO_IDS = (11, 12, 23, 24)
# MediaPipe PoseLandmarker has no hard API cap; this is a high practical max.
MAX_POSES = 20
PERSON_COLORS = (
    (164, 40, 180),
    (40, 90, 200),
    (40, 160, 80),
    (180, 120, 30),
    (200, 60, 60),
    (40, 180, 180),
    (180, 40, 120),
    (100, 100, 220),
    (60, 140, 40),
    (200, 160, 40),
    (120, 60, 200),
    (40, 120, 160),
    (160, 80, 40),
    (80, 80, 80),
    (220, 100, 160),
    (100, 180, 100),
    (80, 140, 220),
    (180, 180, 60),
    (140, 100, 60),
    (60, 60, 160),
)

# Stereo is unreliable closer than this on D456.
MIN_DEPTH_M = 0.35
MAX_DEPTH_M = 6.0
DEPTH_PATCH = 6


class Intrinsics(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("ppx", ctypes.c_float),
        ("ppy", ctypes.c_float),
        ("fx", ctypes.c_float),
        ("fy", ctypes.c_float),
        ("model", ctypes.c_int),
        ("coeffs", ctypes.c_float * 5),
    ]


class RealSense:
    """Minimal librealsense C API wrapper. No pyrealsense2 wheel exists for macOS."""

    def __init__(self, lib_path: str = LIB_PATH) -> None:
        self.lib = ctypes.CDLL(lib_path)
        self._bind()
        self.ctx = None
        self.pipe = None
        self.cfg = None
        self.profile = None
        self.align = None
        self.queue = None
        self.intrinsics: Intrinsics | None = None
        self.depth_scale = 0.001
        self.streaming = False
        self.mode: tuple[int, int, int] | None = None

    def _bind(self) -> None:
        lib = self.lib
        vp = ctypes.c_void_p
        ep = ctypes.POINTER(ctypes.c_void_p)
        lib.rs2_get_error_message.restype = ctypes.c_char_p
        lib.rs2_create_context.restype = vp
        lib.rs2_create_pipeline.restype = vp
        lib.rs2_create_config.restype = vp
        lib.rs2_pipeline_start_with_config.restype = vp
        lib.rs2_pipeline_wait_for_frames.restype = vp
        lib.rs2_create_align.restype = vp
        lib.rs2_create_frame_queue.restype = vp
        lib.rs2_wait_for_frame.restype = vp
        lib.rs2_extract_frame.restype = vp
        lib.rs2_get_frame_stream_profile.restype = vp
        lib.rs2_get_frame_data.restype = ctypes.c_void_p
        lib.rs2_depth_frame_get_distance.restype = ctypes.c_float
        lib.rs2_embedded_frames_count.restype = ctypes.c_int
        lib.rs2_get_frame_width.restype = ctypes.c_int
        lib.rs2_get_frame_height.restype = ctypes.c_int
        for name, args in (
            ("rs2_create_context", [ctypes.c_int, ep]),
            ("rs2_create_pipeline", [vp, ep]),
            ("rs2_create_config", [ep]),
            ("rs2_config_enable_stream", [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ep]),
            ("rs2_pipeline_start_with_config", [vp, vp, ep]),
            ("rs2_pipeline_wait_for_frames", [vp, ctypes.c_uint, ep]),
            ("rs2_create_align", [ctypes.c_int, ep]),
            ("rs2_create_frame_queue", [ctypes.c_int, ep]),
            ("rs2_start_processing_queue", [vp, vp, ep]),
            ("rs2_process_frame", [vp, vp, ep]),
            ("rs2_wait_for_frame", [vp, ctypes.c_uint, ep]),
            ("rs2_embedded_frames_count", [vp, ep]),
            ("rs2_extract_frame", [vp, ctypes.c_int, ep]),
            ("rs2_get_frame_stream_profile", [vp, ep]),
            ("rs2_get_stream_profile_data", [vp, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ep]),
            ("rs2_get_frame_width", [vp, ep]),
            ("rs2_get_frame_height", [vp, ep]),
            ("rs2_get_frame_data", [vp, ep]),
            ("rs2_get_video_stream_intrinsics", [vp, ctypes.POINTER(Intrinsics), ep]),
            ("rs2_depth_frame_get_distance", [vp, ctypes.c_int, ctypes.c_int, ep]),
            ("rs2_release_frame", [vp]),
            ("rs2_pipeline_stop", [vp, ep]),
            ("rs2_delete_context", [vp]),
            ("rs2_delete_pipeline", [vp]),
            ("rs2_delete_config", [vp]),
            ("rs2_delete_pipeline_profile", [vp]),
            ("rs2_delete_processing_block", [vp]),
            ("rs2_delete_frame_queue", [vp]),
        ):
            getattr(lib, name).argtypes = args

    def _err(self):
        return ctypes.c_void_p()

    def _check(self, err: ctypes.c_void_p) -> None:
        if not err.value:
            return
        msg = self.lib.rs2_get_error_message(err)
        text = msg.decode() if msg else "realsense error"
        self.lib.rs2_free_error(err)
        raise RuntimeError(text)

    def _drop(self) -> None:
        lib = self.lib
        if self.streaming and self.pipe:
            err = self._err()
            lib.rs2_pipeline_stop(self.pipe, ctypes.byref(err))
        self.streaming = False
        if self.align:
            lib.rs2_delete_processing_block(self.align)
        if self.queue:
            lib.rs2_delete_frame_queue(self.queue)
        if self.profile:
            lib.rs2_delete_pipeline_profile(self.profile)
        if self.cfg:
            lib.rs2_delete_config(self.cfg)
        if self.pipe:
            lib.rs2_delete_pipeline(self.pipe)
        if self.ctx:
            lib.rs2_delete_context(self.ctx)
        self.ctx = self.pipe = self.cfg = self.profile = self.align = self.queue = None
        self.intrinsics = None

    def start(self, wait_s: float = 45.0) -> None:
        # Taking the camera from macOS UVC often resets USB for a few seconds.
        deadline = time.time() + wait_s
        last = "No device connected"
        while True:
            try:
                self._start_once()
                return
            except RuntimeError as exc:
                last = str(exc)
                self._drop()
                transient = any(
                    key in last
                    for key in (
                        "No device",
                        "failed to set power state",
                        "Device or resource busy",
                        "Cannot open",
                        "Couldn't resolve",
                        "failed to open",
                    )
                )
                if not transient or time.time() >= deadline:
                    raise RuntimeError(last) from exc
                print(f"Waiting for the RealSense ({last})...", file=sys.stderr)
                time.sleep(1.0)

    def _start_once(self) -> None:
        lib = self.lib
        self.streaming = False
        self.intrinsics = None
        err = self._err()
        self.ctx = lib.rs2_create_context(RS2_API_VERSION, ctypes.byref(err))
        self._check(err)
        err = self._err()
        self.pipe = lib.rs2_create_pipeline(self.ctx, ctypes.byref(err))
        self._check(err)

        last = "no matching stream mode"
        for width, height, fps in STREAM_MODES:
            err = self._err()
            self.cfg = lib.rs2_create_config(ctypes.byref(err))
            self._check(err)
            try:
                for stream, fmt in ((RS2_STREAM_DEPTH, RS2_FORMAT_Z16), (RS2_STREAM_COLOR, RS2_FORMAT_BGR8)):
                    err = self._err()
                    lib.rs2_config_enable_stream(
                        self.cfg, stream, -1, width, height, fmt, fps, ctypes.byref(err)
                    )
                    self._check(err)
                err = self._err()
                self.profile = lib.rs2_pipeline_start_with_config(self.pipe, self.cfg, ctypes.byref(err))
                self._check(err)
                self.streaming = True
                self.mode = (width, height, fps)
                break
            except RuntimeError as exc:
                last = str(exc)
                if self.cfg:
                    lib.rs2_delete_config(self.cfg)
                    self.cfg = None
                continue
        else:
            raise RuntimeError(last)

        err = self._err()
        self.align = lib.rs2_create_align(RS2_STREAM_COLOR, ctypes.byref(err))
        self._check(err)
        err = self._err()
        self.queue = lib.rs2_create_frame_queue(2, ctypes.byref(err))
        self._check(err)
        err = self._err()
        lib.rs2_start_processing_queue(self.align, self.queue, ctypes.byref(err))
        self._check(err)
        print(f"Streams {self.mode[0]}x{self.mode[1]}@{self.mode[2]} aligned to color")

    def stop(self) -> None:
        self._drop()

    def reopen(self) -> None:
        print("Reopening RealSense...", file=sys.stderr)
        self._drop()
        time.sleep(1.0)
        self.start(wait_s=30.0)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.streaming or not self.pipe:
            raise RuntimeError("wait_for_frames cannot be called before start()")
        lib = self.lib
        err = self._err()
        frames = lib.rs2_pipeline_wait_for_frames(self.pipe, 5000, ctypes.byref(err))
        self._check(err)
        err = self._err()
        try:
            lib.rs2_process_frame(self.align, frames, ctypes.byref(err))
            self._check(err)
            frames = None  # ownership moved into the align block
        except Exception:
            if frames:
                lib.rs2_release_frame(frames)
            raise
        err = self._err()
        aligned = lib.rs2_wait_for_frame(self.queue, 5000, ctypes.byref(err))
        self._check(err)
        try:
            return self._split(aligned)
        finally:
            lib.rs2_release_frame(aligned)

    def _split(self, composite) -> tuple[np.ndarray, np.ndarray]:
        lib = self.lib
        err = self._err()
        count = lib.rs2_embedded_frames_count(composite, ctypes.byref(err))
        self._check(err)
        color = depth = None
        need_meta = self.intrinsics is None
        for index in range(count):
            err = self._err()
            frame = lib.rs2_extract_frame(composite, index, ctypes.byref(err))
            self._check(err)
            try:
                err = self._err()
                profile = lib.rs2_get_frame_stream_profile(frame, ctypes.byref(err))
                self._check(err)
                stream = ctypes.c_int()
                fmt = ctypes.c_int()
                sindex = ctypes.c_int()
                uid = ctypes.c_int()
                fps = ctypes.c_int()
                err = self._err()
                lib.rs2_get_stream_profile_data(
                    profile,
                    ctypes.byref(stream),
                    ctypes.byref(fmt),
                    ctypes.byref(sindex),
                    ctypes.byref(uid),
                    ctypes.byref(fps),
                    ctypes.byref(err),
                )
                self._check(err)
                err = self._err()
                width = lib.rs2_get_frame_width(frame, ctypes.byref(err))
                self._check(err)
                err = self._err()
                height = lib.rs2_get_frame_height(frame, ctypes.byref(err))
                self._check(err)
                err = self._err()
                data = lib.rs2_get_frame_data(frame, ctypes.byref(err))
                self._check(err)
                if stream.value == RS2_STREAM_DEPTH:
                    raw = ctypes.string_at(data, width * height * 2)
                    depth = np.frombuffer(raw, dtype=np.uint16).reshape(height, width).copy()
                    if need_meta:
                        # Must read scale while this frame is still alive.
                        self.depth_scale = self._scale_from_frame(frame, depth)
                elif stream.value == RS2_STREAM_COLOR:
                    raw = ctypes.string_at(data, width * height * 3)
                    color = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
                    if need_meta:
                        # Align-to-color: deproject with the color camera model.
                        # Profile is owned by the frame — read before release.
                        intr = Intrinsics()
                        err = self._err()
                        lib.rs2_get_video_stream_intrinsics(
                            profile, ctypes.byref(intr), ctypes.byref(err)
                        )
                        self._check(err)
                        self.intrinsics = intr
            finally:
                lib.rs2_release_frame(frame)

        if color is None or depth is None:
            raise RuntimeError("aligned frameset is missing color or depth")
        if color.shape[:2] != depth.shape[:2]:
            raise RuntimeError(
                f"aligned size mismatch color={color.shape[:2]} depth={depth.shape[:2]}"
            )
        if need_meta:
            if self.intrinsics is None:
                raise RuntimeError("missing color profile for intrinsics")
            intr = self.intrinsics
            print(
                f"Intrinsics {intr.width}x{intr.height}  "
                f"fx={intr.fx:.1f} fy={intr.fy:.1f}  "
                f"pp=({intr.ppx:.1f},{intr.ppy:.1f})  "
                f"depth_scale={self.depth_scale:.6f}"
            )
        return color, depth

    def _scale_from_frame(self, frame, depth: np.ndarray) -> float:
        ys, xs = np.nonzero(depth)
        if len(xs) == 0:
            return 0.001
        # Prefer a central valid pixel; edge samples are often noisy.
        h, w = depth.shape
        cy, cx = h // 2, w // 2
        order = np.argsort((ys - cy) ** 2 + (xs - cx) ** 2)
        for idx in order[:200]:
            x = int(xs[idx])
            y = int(ys[idx])
            err = self._err()
            dist = float(self.lib.rs2_depth_frame_get_distance(frame, x, y, ctypes.byref(err)))
            if err.value:
                self.lib.rs2_free_error(err)
                continue
            raw = int(depth[y, x])
            if raw > 0 and dist > 0:
                return dist / raw
        return 0.001


def depth_at(depth: np.ndarray, u: int, v: int, scale: float, radius: int = DEPTH_PATCH) -> float:
    h, w = depth.shape
    patch = depth[max(0, v - radius) : min(h, v + radius + 1), max(0, u - radius) : min(w, u + radius + 1)]
    valid = patch[patch > 0]
    if valid.size == 0:
        return 0.0
    return float(np.median(valid)) * scale


def lift_person(landmarks, depth: np.ndarray, intr: Intrinsics, scale: float) -> list[dict]:
    h, w = depth.shape
    joints = []
    for index, lm in enumerate(landmarks):
        u = int(np.clip(round(lm.x * w), 0, w - 1))
        v = int(np.clip(round(lm.y * h), 0, h - 1))
        z = depth_at(depth, u, v, scale)
        visible = float(getattr(lm, "visibility", 1.0) or 0.0)
        if z < MIN_DEPTH_M or z > MAX_DEPTH_M or visible < 0.5:
            joints.append({"name": NAMES[index], "ok": False, "x": None, "y": None, "z": None, "u": u, "v": v})
            continue
        x = (u - intr.ppx) * z / intr.fx
        y = (v - intr.ppy) * z / intr.fy
        joints.append({
            "name": NAMES[index],
            "ok": True,
            "x": round(x, 4),
            "y": round(y, 4),
            "z": round(z, 4),
            "u": u,
            "v": v,
            "visibility": round(visible, 3),
        })
    return joints


def is_real_person(joints: list[dict]) -> bool:
    """Drop face-only / junk detections; need a torso with depth."""
    torso = sum(1 for i in TORSO_IDS if joints[i]["ok"])
    if torso < 2:
        return False
    zs = [j["z"] for j in joints if j["ok"]]
    if not zs:
        return False
    # A body spanning the near clip almost always means bad stereo / too close.
    return float(np.median(zs)) >= 0.45


def world_points(people: list[list[dict]]) -> tuple[list[np.ndarray], float]:
    feet = []
    for person in people:
        for joint in person:
            if joint["ok"] and NAMES.index(joint["name"]) in FOOT_IDS:
                feet.append(joint["y"])
    if not feet:
        ys = [j["y"] for person in people for j in person if j["ok"]]
        floor_cam_y = max(ys) if ys else 0.0
    else:
        floor_cam_y = max(feet)
    clouds = []
    for person in people:
        pts = np.full((len(NAMES), 3), np.nan, dtype=np.float64)
        for index, joint in enumerate(person):
            if not joint["ok"]:
                continue
            # Camera Y down -> display Y up, feet on the floor grid.
            pts[index] = (joint["x"], floor_cam_y - joint["y"], joint["z"])
        clouds.append(pts)
    return clouds, floor_cam_y


def project(points: np.ndarray, eye, right, up, forward, f: float, cx: int, cy: int) -> list[tuple[int, int] | None]:
    out = []
    for p in points:
        if not np.isfinite(p).all():
            out.append(None)
            continue
        v = p - eye
        z = float(np.dot(v, forward))
        if z < 0.15:
            out.append(None)
            continue
        x = float(np.dot(v, right))
        y = float(np.dot(v, up))
        out.append((int(cx + f * x / z), int(cy - f * y / z)))
    return out


def draw_grid(canvas: np.ndarray, eye, right, up, forward) -> None:
    h, w = canvas.shape[:2]
    f, cx, cy = 420.0, w // 2, int(h * 0.62)

    def one(p):
        v = p - eye
        z = float(np.dot(v, forward))
        if z < 0.15:
            return None
        return int(cx + f * float(np.dot(v, right)) / z), int(cy - f * float(np.dot(v, up)) / z)

    color = (210, 210, 210)
    for x in np.arange(-2.0, 2.01, 0.5):
        a, b = one(np.array([x, 0.0, 0.4])), one(np.array([x, 0.0, 4.0]))
        if a and b:
            cv2.line(canvas, a, b, color, 1, cv2.LINE_AA)
    for z in np.arange(0.5, 4.01, 0.5):
        a, b = one(np.array([-2.0, 0.0, z])), one(np.array([2.0, 0.0, z]))
        if a and b:
            cv2.line(canvas, a, b, color, 1, cv2.LINE_AA)


def render(color: np.ndarray, people: list[list[dict]]) -> tuple[np.ndarray, np.ndarray]:
    """Return (color overlay, 3D stick view) as two separate images."""
    view = color.copy()
    for pid, person in enumerate(people):
        bgr = PERSON_COLORS[pid % len(PERSON_COLORS)]
        for a, b in BONES:
            ja, jb = person[a], person[b]
            if ja["ok"] and jb["ok"]:
                cv2.line(view, (ja["u"], ja["v"]), (jb["u"], jb["v"]), bgr, 2, cv2.LINE_AA)
        for joint in person:
            if joint["ok"]:
                cv2.circle(view, (joint["u"], joint["v"]), 3, bgr, -1, cv2.LINE_AA)

    pane = np.full_like(view, 255)
    clouds, _floor = world_points(people)
    eye = np.array([1.5, 1.35, 0.15], dtype=np.float64)
    target = np.array([0.0, 0.9, 2.2], dtype=np.float64)
    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(np.array([0.0, 1.0, 0.0]), forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    draw_grid(pane, eye, right, up, forward)
    h, w = pane.shape[:2]
    for pid, cloud in enumerate(clouds):
        bgr = PERSON_COLORS[pid % len(PERSON_COLORS)]
        pix = project(cloud, eye, right, up, forward, 460.0, w // 2, int(h * 0.72))
        for a, b in BONES:
            if pix[a] and pix[b]:
                cv2.line(pane, pix[a], pix[b], bgr, 3, cv2.LINE_AA)
        for p in pix:
            if p:
                cv2.circle(pane, p, 4, bgr, -1, cv2.LINE_AA)
    cv2.putText(view, f"people {len(people)}", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return view, pane


def write_joints(people: list[list[dict]], depth_scale: float, mode: tuple[int, int, int] | None) -> None:
    payload = {
        "camera": "Intel RealSense D456",
        "frame": "camera",
        "units": "meters",
        "axes": "x right, y down, z forward",
        "depth_scale": depth_scale,
        "stream": {"width": mode[0], "height": mode[1], "fps": mode[2]} if mode else None,
        "people": people,
    }
    text = json.dumps(payload, indent=2)
    OUT_JSON.write_text(text)
    # sudo makes the file root-owned otherwise; keep it editable by the user.
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(OUT_JSON, int(uid), int(gid))
        except OSError:
            pass


def make_landmarker():
    import mediapipe as mp

    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(MODEL)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=MAX_POSES,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return mp, mp.tasks.vision.PoseLandmarker.create_from_options(options)


def _fatal_stream_error(message: str) -> bool:
    keys = (
        "before start",
        "No device",
        "failed to set power state",
        "Device or resource busy",
        "Camera disconnected",
        "xioctl",
        "Cannot open",
        "Frame didn't arrive",
    )
    return any(k.lower() in message.lower() for k in keys)


def main() -> int:
    if not MODEL.exists():
        print(f"missing model: {MODEL}", file=sys.stderr)
        return 1
    if os.geteuid() != 0:
        print("Run via ./run_live.sh (needs root for librealsense on macOS).", file=sys.stderr)
        print(f"  {HERE}/run_live.sh", file=sys.stderr)
        return 1

    mp, landmarker = make_landmarker()
    cam = RealSense()
    print("Opening RealSense D456...")
    cam.start()
    print("Streaming. Press q to quit.")
    limit = int(os.environ.get("REALSENSE_FRAMES", "0") or "0")
    frames = 0
    misses = 0
    reconnects = 0
    try:
        while True:
            try:
                color, depth = cam.read()
            except RuntimeError as exc:
                misses += 1
                print(exc, file=sys.stderr)
                if _fatal_stream_error(str(exc)):
                    if reconnects >= 5:
                        print("gave up reopening the camera", file=sys.stderr)
                        return 1
                    reconnects += 1
                    try:
                        cam.reopen()
                        misses = 0
                        continue
                    except RuntimeError as reopen_exc:
                        print(reopen_exc, file=sys.stderr)
                        return 1
                if misses > 60:
                    print("too many dropped frames", file=sys.stderr)
                    return 1
                continue
            misses = 0
            rgb = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
            stamp = frames * 33 + 1
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect_for_video(image, stamp)
            assert cam.intrinsics is not None
            people = []
            for pose in result.pose_landmarks or []:
                joints = lift_person(pose, depth, cam.intrinsics, cam.depth_scale)
                if is_real_person(joints):
                    people.append(joints)
            if frames % 5 == 0:
                write_joints(people, cam.depth_scale, cam.mode)
            color_view, joints_3d = render(color, people)
            frames += 1
            if frames == 1 or frames % 30 == 0:
                ok = sum(j["ok"] for person in people for j in person)
                print(f"frame {frames}  people {len(people)}  joints {ok}")
            if limit and frames >= limit:
                preview_color = HERE / "preview_color.jpg"
                preview_3d = HERE / "preview_3d.jpg"
                cv2.imwrite(str(preview_color), color_view)
                cv2.imwrite(str(preview_3d), joints_3d)
                uid = os.environ.get("SUDO_UID")
                gid = os.environ.get("SUDO_GID")
                if uid and gid:
                    for path in (preview_color, preview_3d):
                        try:
                            os.chown(path, int(uid), int(gid))
                        except OSError:
                            pass
                print(f"wrote {preview_color}")
                print(f"wrote {preview_3d}")
                break
            cv2.imshow("RealSense color", color_view)
            cv2.imshow("RealSense 3D joints", joints_3d)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cam.stop()
        landmarker.close()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
