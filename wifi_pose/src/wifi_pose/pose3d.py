"""MediaPipe pose → depth-lifted 3D joints (camera meters)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wifi_pose.realsense import Intrinsics

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
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 7),
    (0, 4),
    (4, 5),
    (5, 6),
    (6, 8),
    (9, 10),
    (11, 12),
    (11, 13),
    (13, 15),
    (15, 17),
    (15, 19),
    (15, 21),
    (17, 19),
    (12, 14),
    (14, 16),
    (16, 18),
    (16, 20),
    (16, 22),
    (18, 20),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (25, 27),
    (27, 29),
    (27, 31),
    (29, 31),
    (24, 26),
    (26, 28),
    (28, 30),
    (28, 32),
    (30, 32),
]
FOOT_IDS = (27, 28, 29, 30, 31, 32)
TORSO_IDS = (11, 12, 23, 24)
MAX_POSES = 4
MIN_DEPTH_M = 0.35
MAX_DEPTH_M = 6.0
DEPTH_PATCH = 6


def depth_at(
    depth: np.ndarray, u: int, v: int, scale: float, radius: int = DEPTH_PATCH
) -> float:
    h, w = depth.shape
    patch = depth[
        max(0, v - radius) : min(h, v + radius + 1),
        max(0, u - radius) : min(w, u + radius + 1),
    ]
    valid = patch[patch > 0]
    if valid.size == 0:
        return 0.0
    return float(np.median(valid)) * scale


def lift_person(
    landmarks, depth: np.ndarray, intr: Intrinsics, scale: float
) -> list[dict]:
    h, w = depth.shape
    joints = []
    for index, lm in enumerate(landmarks):
        u = int(np.clip(round(lm.x * w), 0, w - 1))
        v = int(np.clip(round(lm.y * h), 0, h - 1))
        z = depth_at(depth, u, v, scale)
        visible = float(getattr(lm, "visibility", 1.0) or 0.0)
        if z < MIN_DEPTH_M or z > MAX_DEPTH_M or visible < 0.5:
            joints.append(
                {
                    "name": NAMES[index],
                    "ok": False,
                    "x": None,
                    "y": None,
                    "z": None,
                    "u": u,
                    "v": v,
                }
            )
            continue
        x = (u - intr.ppx) * z / intr.fx
        y = (v - intr.ppy) * z / intr.fy
        joints.append(
            {
                "name": NAMES[index],
                "ok": True,
                "x": round(x, 4),
                "y": round(y, 4),
                "z": round(z, 4),
                "u": u,
                "v": v,
                "visibility": round(visible, 3),
            }
        )
    return joints


def is_real_person(joints: list[dict]) -> bool:
    torso = sum(1 for i in TORSO_IDS if joints[i]["ok"])
    if torso < 2:
        return False
    zs = [j["z"] for j in joints if j["ok"]]
    if not zs:
        return False
    return float(np.median(zs)) >= 0.45


def make_landmarker(model_path: Path):
    import mediapipe as mp

    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path)),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=MAX_POSES,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    return mp, mp.tasks.vision.PoseLandmarker.create_from_options(options)
