"""Color overlay + simple 3D stick-figure pane."""

from __future__ import annotations

import cv2
import numpy as np

from wifi_pose.pose3d import BONES, FOOT_IDS, NAMES

PERSON_COLORS = (
    (164, 40, 180),
    (40, 90, 200),
    (40, 160, 80),
    (180, 120, 30),
)


def world_points(people: list[list[dict]]) -> list[np.ndarray]:
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
            pts[index] = (joint["x"], floor_cam_y - joint["y"], joint["z"])
        clouds.append(pts)
    return clouds


def project(points, eye, right, up, forward, f, cx, cy):
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


def draw_grid(canvas, eye, right, up, forward) -> None:
    h, w = canvas.shape[:2]
    f, cx, cy = 420.0, w // 2, int(h * 0.62)

    def one(p):
        v = p - eye
        z = float(np.dot(v, forward))
        if z < 0.15:
            return None
        return (
            int(cx + f * float(np.dot(v, right)) / z),
            int(cy - f * float(np.dot(v, up)) / z),
        )

    color = (210, 210, 210)
    for x in np.arange(-2.0, 2.01, 0.5):
        a, b = one(np.array([x, 0.0, 0.4])), one(np.array([x, 0.0, 4.0]))
        if a and b:
            cv2.line(canvas, a, b, color, 1, cv2.LINE_AA)
    for z in np.arange(0.5, 4.01, 0.5):
        a, b = one(np.array([-2.0, 0.0, z])), one(np.array([2.0, 0.0, z]))
        if a and b:
            cv2.line(canvas, a, b, color, 1, cv2.LINE_AA)


def render(color: np.ndarray, people: list[list[dict]], frame_i: int = 0) -> np.ndarray:
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
    clouds = world_points(people)
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
    cv2.putText(
        pane, "3D joints, meters", (16, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 40), 2, cv2.LINE_AA
    )
    cv2.putText(
        view,
        f"frame {frame_i}  people {len(people)}",
        (16, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    if not people:
        cv2.putText(
            view,
            "stand 1-2 m in front",
            (16, 56),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return np.hstack([view, pane])
