"""Healthcare activity from COCO-17 pose keypoints + short temporal window."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np

# COCO-17 used by Ultralytics YOLO-pose
NOSE, L_SHO, R_SHO, L_HIP, R_HIP = 0, 5, 6, 11, 12
L_KNEE, R_KNEE, L_ANK, R_ANK = 13, 14, 15, 16


@dataclass
class ActivityState:
    label: str = "unknown"
    confidence: float = 0.0
    detail: str = ""


def _ok(kpts: np.ndarray, idx: int, thr: float = 0.25) -> bool:
    if kpts is None or idx >= len(kpts):
        return False
    if kpts.shape[1] >= 3:
        return float(kpts[idx, 2]) >= thr
    return True


def _pt(kpts: np.ndarray, idx: int) -> Optional[np.ndarray]:
    if not _ok(kpts, idx):
        return None
    return kpts[idx, :2].astype(float)


def _pt_thr(kpts: np.ndarray, idx: int, thr: float) -> Optional[np.ndarray]:
    if not _ok(kpts, idx, thr=thr):
        return None
    return kpts[idx, :2].astype(float)


def _mid(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if a is None or b is None:
        return None
    return (a + b) * 0.5


def _angle_deg(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Angle at b (degrees) for points a-b-c."""
    ba = a - b
    bc = c - b
    nba = np.linalg.norm(ba)
    nbc = np.linalg.norm(bc)
    if nba < 1e-6 or nbc < 1e-6:
        return float("nan")
    cos = float(np.clip(np.dot(ba, bc) / (nba * nbc), -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def _torso_tilt_deg(shoulder: np.ndarray, hip: np.ndarray) -> float:
    """0° ≈ vertical (standing), 90° ≈ horizontal (lying)."""
    v = shoulder - hip
    # angle from vertical up-axis (0, -1) in image coords (y down)
    vertical = np.array([0.0, -1.0])
    nv = np.linalg.norm(v)
    if nv < 1e-6:
        return float("nan")
    cos = float(np.clip(np.dot(v / nv, vertical), -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def _classify_upper_body(
    nose: Optional[np.ndarray],
    shoulder: np.ndarray,
    box: Optional[tuple[float, float, float, float]],
    frame_hw: Optional[tuple[int, int]],
) -> ActivityState:
    """
    No hips visible (close webcam). Do NOT always assume sitting —
    use box height in the frame: tall near-top box ≈ standing close-up,
    shorter mid box ≈ sitting at desk.
    """
    if box is not None and frame_hw is not None:
        fh = max(1, int(frame_hw[0]))
        x1, y1, x2, y2 = box
        box_h = (y2 - y1) / fh
        top = y1 / fh
        # Standing close to camera: fills most of vertical FOV, head near top
        if box_h >= 0.58 and top <= 0.22:
            return ActivityState("standing", 0.62, f"upper_body tall_box h={box_h:.2f}")
        # Sitting / desk: more headroom or shorter crop
        if box_h <= 0.52 or top >= 0.18:
            return ActivityState("sitting", 0.55, f"upper_body desk_box h={box_h:.2f}")
    if nose is not None and nose[1] < shoulder[1]:
        return ActivityState("unknown", 0.4, "upper_body_only (step back for legs)")
    return ActivityState("unknown", 0.35, "upper_body_only")


def classify_frame(
    kpts: np.ndarray,
    box: Optional[tuple[float, float, float, float]] = None,
    frame_hw: Optional[tuple[int, int]] = None,
) -> ActivityState:
    """Single-frame posture guess from one person keypoints (N,2|3)."""
    if kpts is None or len(kpts) < 17:
        return ActivityState("unknown", 0.0, "no_pose")

    nose = _pt(kpts, NOSE)
    l_sho, r_sho = _pt(kpts, L_SHO), _pt(kpts, R_SHO)
    l_hip, r_hip = _pt(kpts, L_HIP), _pt(kpts, R_HIP)
    l_knee, r_knee = _pt(kpts, L_KNEE), _pt(kpts, R_KNEE)
    l_ank, r_ank = _pt(kpts, L_ANK), _pt(kpts, R_ANK)

    shoulder = _mid(l_sho, r_sho)
    hip = _mid(l_hip, r_hip)

    # Close-up: hips missing — use box geometry, not blind "sitting"
    if shoulder is not None and hip is None:
        return _classify_upper_body(nose, shoulder, box, frame_hw)

    if shoulder is None or hip is None:
        return ActivityState("unknown", 0.0, "missing_torso")

    tilt = _torso_tilt_deg(shoulder, hip)
    torso_len = float(np.linalg.norm(shoulder - hip)) + 1e-6

    # Knee angles (bent ≈ sitting)
    knee_angles = []
    for hip_i, knee_i, ank_i in (
        (l_hip, l_knee, l_ank),
        (r_hip, r_knee, r_ank),
    ):
        if hip_i is not None and knee_i is not None and ank_i is not None:
            knee_angles.append(_angle_deg(hip_i, knee_i, ank_i))
    knee = float(np.nanmean(knee_angles)) if knee_angles else float("nan")

    # Hip height vs shoulder (image y increases downward)
    hip_drop = float((hip[1] - shoulder[1]) / torso_len)

    # Lying: torso near horizontal
    if not np.isnan(tilt) and tilt >= 55:
        return ActivityState("lying", min(0.95, 0.55 + (tilt - 55) / 80), f"tilt={tilt:.0f}")

    # Sitting: hips clearly below shoulders, knees bent
    if hip_drop > 0.55 and (np.isnan(knee) or knee < 145):
        conf = 0.6
        if not np.isnan(knee) and knee < 120:
            conf = 0.85
        return ActivityState("sitting", conf, f"drop={hip_drop:.2f} knee={knee:.0f}")

    # Standing-like
    if not np.isnan(tilt) and tilt < 35 and hip_drop > 0.35:
        conf = 0.75 if (np.isnan(knee) or knee > 140) else 0.55
        return ActivityState("standing", conf, f"tilt={tilt:.0f} knee={knee:.0f}")

    return ActivityState("unknown", 0.3, f"tilt={tilt:.0f} drop={hip_drop:.2f}")


class ActivityTracker:
    """Smooth labels + walking / fall_suspected from short history."""

    def __init__(self, window: int = 18, fall_frames: int = 8):
        self.window = window
        self.fall_frames = fall_frames
        self.labels: Deque[str] = deque(maxlen=window)
        self.hips: Deque[np.ndarray] = deque(maxlen=window)
        self.tilts: Deque[float] = deque(maxlen=window)
        self._last = ActivityState()

    def update(
        self,
        kpts: np.ndarray,
        box: Optional[tuple[float, float, float, float]] = None,
        frame_hw: Optional[tuple[int, int]] = None,
    ) -> ActivityState:
        frame = classify_frame(kpts, box=box, frame_hw=frame_hw)
        # Stricter hips for walking (ignore weak/jittery hip guesses)
        hip = _mid(_pt_thr(kpts, L_HIP, 0.45), _pt_thr(kpts, R_HIP, 0.45))
        sho = _mid(_pt(kpts, L_SHO), _pt(kpts, R_SHO))
        tilt = _torso_tilt_deg(sho, hip) if sho is not None and hip is not None else float("nan")
        upper_only = "upper_body" in (frame.detail or "")

        self.labels.append(frame.label)
        if hip is not None:
            self.hips.append(hip.copy())
        else:
            # Close-up / no hips: clear stale hip path so we don't fake "walking"
            self.hips.clear()
        if not np.isnan(tilt):
            self.tilts.append(tilt)

        # Fall: recent upright-ish then sudden high tilt + hip drop in y
        fall = self._fall_suspected()
        if fall:
            st = ActivityState("fall_suspected", 0.8, fall)
            self._last = st
            return st

        # Walking only with real hips visible this frame (not close-up wobble)
        if (not upper_only) and hip is not None and self._is_walking():
            st = ActivityState("walking", 0.7, "motion+standing")
            self._last = st
            return st

        # Close-up standing: trust current frame, don't majority-vote into walking/sitting
        if upper_only and frame.label == "standing":
            self._last = frame
            return frame

        # Majority vote over window
        if self.labels:
            vals, counts = np.unique(np.array(self.labels), return_counts=True)
            maj = str(vals[int(np.argmax(counts))])
            # Never let majority invent "walking" without the motion check above
            if maj == "walking":
                maj = "standing" if frame.label in ("standing", "walking", "unknown") else frame.label
            conf = float(counts.max() / len(self.labels))
            if maj == "unknown" and frame.label != "unknown":
                st = frame
            else:
                st = ActivityState(maj, max(frame.confidence, conf * 0.9), frame.detail)
            self._last = st
            return st

        self._last = frame
        return frame

    def _is_walking(self) -> bool:
        if len(self.labels) < self.window // 2:
            return False
        standingish = sum(1 for x in self.labels if x in ("standing", "walking"))
        if standingish < len(self.labels) * 0.6:
            return False
        if len(self.hips) < 10:
            return False
        arr = np.stack(list(self.hips), axis=0)
        dx = float(np.std(arr[:, 0]))
        dy = float(np.std(arr[:, 1]))
        # Need clear lateral travel — camera shake alone is usually smaller / isotropic
        return dx > 18.0 and dx > dy * 1.15

    def _fall_suspected(self) -> str:
        # Need upright history *before* the recent window, then horizontal now.
        need = self.fall_frames * 2
        if len(self.tilts) < need or len(self.hips) < need:
            return ""
        tilts = list(self.tilts)
        hips = list(self.hips)
        past = float(np.mean(tilts[-need : -self.fall_frames]))
        now = float(np.mean(tilts[-max(2, self.fall_frames // 3) :]))
        dy = float(hips[-1][1] - hips[-need][1])
        # upright -> horizontal; hips usually move down the image (optional boost)
        if past < 40 and now > 55 and dy > 15:
            return f"tilt {past:.0f}->{now:.0f} dy={dy:.0f}"
        if past < 35 and now > 60:
            # strong tilt change alone (bed fall / camera angle)
            return f"tilt {past:.0f}->{now:.0f}"
        return ""
