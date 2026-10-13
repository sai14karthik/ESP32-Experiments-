"""
Healthcare activity from YOLO-pose COCO-17 keypoints.

Rules aligned with common open real-time pipelines (YOLO-pose + geometry):
  - bbox aspect ratio → lying vs upright (pose-estimation-task / fall papers)
  - hip–knee–ankle & shoulder–hip–knee angles → sitting vs standing
    (YOLOv8 HAR angle demos; MediaPipe / ML Kit angle heuristics)
  - normalized centroid velocity over a short window → walking
  - upright → horizontal + drop → fall_suspected (temporal, not single frame)

Pose keypoints themselves come from Ultralytics YOLO26-pose (not this file).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np

NOSE, L_SHO, R_SHO, L_HIP, R_HIP = 0, 5, 6, 11, 12
L_KNEE, R_KNEE, L_ANK, R_ANK = 13, 14, 15, 16

# Full healthcare label set (live HUD + roles must cover all of these)
ALL_ACTIVITIES = (
    "standing",
    "sitting",
    "walking",
    "lying",
    "fall_suspected",
    "unknown",
)


@dataclass
class ActivityState:
    label: str = "unknown"
    confidence: float = 0.0
    detail: str = ""


def _ok(kpts: np.ndarray, idx: int, thr: float = 0.4) -> bool:
    if kpts is None or idx >= len(kpts):
        return False
    if kpts.shape[1] >= 3:
        return float(kpts[idx, 2]) >= thr
    return True


def _pt(kpts: np.ndarray, idx: int, thr: float = 0.4) -> Optional[np.ndarray]:
    if not _ok(kpts, idx, thr=thr):
        return None
    return kpts[idx, :2].astype(float)


def _mid(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if a is None or b is None:
        return None
    return (a + b) * 0.5


def _angle_deg(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    ba = a - b
    bc = c - b
    nba = np.linalg.norm(ba)
    nbc = np.linalg.norm(bc)
    if nba < 1e-6 or nbc < 1e-6:
        return float("nan")
    cos = float(np.clip(np.dot(ba, bc) / (nba * nbc), -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def _torso_tilt_deg(shoulder: np.ndarray, hip: np.ndarray) -> float:
    """0° ≈ vertical upright, 90° ≈ horizontal (lying)."""
    v = shoulder - hip
    vertical = np.array([0.0, -1.0])
    nv = np.linalg.norm(v)
    if nv < 1e-6:
        return float("nan")
    cos = float(np.clip(np.dot(v / nv, vertical), -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def _mean_angle(angles: list[float]) -> float:
    vals = [a for a in angles if not np.isnan(a)]
    return float(np.mean(vals)) if vals else float("nan")


def classify_frame(
    kpts: np.ndarray,
    box: Optional[tuple[float, float, float, float]] = None,
    frame_hw: Optional[tuple[int, int]] = None,
) -> ActivityState:
    """
    Single-frame posture (before temporal walking/fall).
    Priority: lying (aspect/tilt) → sitting (knee/hip angles) → standing.
    """
    if kpts is None or len(kpts) < 17:
        return ActivityState("unknown", 0.0, "no_pose")

    # --- bbox aspect (strong lying / upright cues) ---
    aspect = float("nan")
    box_h = float("nan")
    if box is not None:
        x1, y1, x2, y2 = box
        bw = max(1.0, x2 - x1)
        bh = max(1.0, y2 - y1)
        aspect = bw / bh
        box_h = bh
        # Lying: wide box (published rule: w > 1.5 * h)
        if aspect >= 1.5:
            return ActivityState("lying", 0.9, f"bbox_aspect={aspect:.2f}")

    nose = _pt(kpts, NOSE)
    l_sho, r_sho = _pt(kpts, L_SHO), _pt(kpts, R_SHO)
    l_hip, r_hip = _pt(kpts, L_HIP), _pt(kpts, R_HIP)
    l_knee, r_knee = _pt(kpts, L_KNEE), _pt(kpts, R_KNEE)
    l_ank, r_ank = _pt(kpts, L_ANK), _pt(kpts, R_ANK)

    shoulder = _mid(l_sho, r_sho)
    hip = _mid(l_hip, r_hip)

    # Close-up without hips: limited confidence — prefer unknown over wrong sit
    if shoulder is not None and hip is None:
        if box is not None and frame_hw is not None:
            fh = max(1, int(frame_hw[0]))
            x1, y1, x2, y2 = box
            rel_h = (y2 - y1) / fh
            top = y1 / fh
            if rel_h >= 0.6 and top <= 0.2:
                return ActivityState("standing", 0.55, "upper_body close-up")
            if rel_h <= 0.5:
                return ActivityState("sitting", 0.5, "upper_body desk-crop")
        return ActivityState("unknown", 0.35, "need_full_body (step back)")

    if shoulder is None or hip is None:
        return ActivityState("unknown", 0.0, "missing_torso")

    tilt = _torso_tilt_deg(shoulder, hip)

    # Lying by torso orientation
    if not np.isnan(tilt) and tilt >= 55:
        conf = min(0.95, 0.6 + (tilt - 55) / 70)
        return ActivityState("lying", conf, f"torso_tilt={tilt:.0f}")

    # Knee angles (hip–knee–ankle) — sitting / squatting when bent
    knee_angles = []
    for hi, kn, an in ((l_hip, l_knee, l_ank), (r_hip, r_knee, r_ank)):
        if hi is not None and kn is not None and an is not None:
            knee_angles.append(_angle_deg(hi, kn, an))
    knee = _mean_angle(knee_angles)

    # Hip angles (shoulder–hip–knee) — HAR demos use ~110° sit threshold
    hip_angles = []
    for sh, hi, kn in ((l_sho, l_hip, l_knee), (r_sho, r_hip, r_knee)):
        if sh is not None and hi is not None and kn is not None:
            hip_angles.append(_angle_deg(sh, hi, kn))
    hip_ang = _mean_angle(hip_angles)

    # Nose–hip–knee (alternate sit cue when shoulders noisy)
    nhk = float("nan")
    if nose is not None and hip is not None:
        for kn in (l_knee, r_knee):
            if kn is not None:
                a = _angle_deg(nose, hip, kn)
                if not np.isnan(a):
                    nhk = a if np.isnan(nhk) else 0.5 * (nhk + a)

    # Sitting: bent knees OR bent hip
    if (not np.isnan(knee) and knee < 130) or (not np.isnan(hip_ang) and hip_ang < 115):
        conf = 0.7
        if not np.isnan(knee) and knee < 110:
            conf = 0.88
        return ActivityState(
            "sitting",
            conf,
            f"knee={knee:.0f} hip_ang={hip_ang:.0f}",
        )
    if not np.isnan(nhk) and nhk < 110:
        return ActivityState("sitting", 0.72, f"nose_hip_knee={nhk:.0f}")

    # Standing: upright bbox and/or straight legs
    upright_box = (not np.isnan(aspect) and aspect < 0.65) or (
        not np.isnan(tilt) and tilt < 40
    )
    straight_legs = np.isnan(knee) or knee >= 140
    if upright_box and straight_legs:
        conf = 0.8 if (not np.isnan(knee) and knee >= 150) else 0.65
        return ActivityState("standing", conf, f"tilt={tilt:.0f} knee={knee:.0f} asp={aspect:.2f}")

    if upright_box:
        return ActivityState("standing", 0.55, f"upright_box asp={aspect:.2f}")

    return ActivityState("unknown", 0.3, f"tilt={tilt:.0f} knee={knee:.0f} asp={aspect:.2f}")


class ActivityTracker:
    """Temporal smoothing + walking / fall_suspected for all ALL_ACTIVITIES labels."""

    def __init__(
        self,
        window: int = 20,
        fall_frames: int = 8,
        walk_speed: float = 0.022,  # fraction of bbox height per frame
        walk_confirm: int = 2,
    ):
        self.window = window
        self.fall_frames = fall_frames
        self.walk_speed = walk_speed
        self.walk_confirm = walk_confirm
        self.labels: Deque[str] = deque(maxlen=window)
        self.hips: Deque[np.ndarray] = deque(maxlen=window)
        self.tilts: Deque[float] = deque(maxlen=window)
        self.centers: Deque[np.ndarray] = deque(maxlen=12)
        self.box_hs: Deque[float] = deque(maxlen=12)
        self.ankles: Deque[np.ndarray] = deque(maxlen=12)
        self.walk_hits = 0
        self._ema: dict[str, float] = {}
        self._last = ActivityState()

    def update(
        self,
        kpts: np.ndarray,
        box: Optional[tuple[float, float, float, float]] = None,
        frame_hw: Optional[tuple[int, int]] = None,
    ) -> ActivityState:
        frame = classify_frame(kpts, box=box, frame_hw=frame_hw)
        hip = _mid(_pt(kpts, L_HIP, 0.35), _pt(kpts, R_HIP, 0.35))
        sho = _mid(_pt(kpts, L_SHO), _pt(kpts, R_SHO))
        ank = _mid(_pt(kpts, L_ANK, 0.3), _pt(kpts, R_ANK, 0.3))
        tilt = (
            _torso_tilt_deg(sho, hip)
            if sho is not None and hip is not None
            else float("nan")
        )
        upper_only = "upper_body" in (frame.detail or "") or "need_full_body" in (
            frame.detail or ""
        )

        self.labels.append(frame.label)
        if hip is not None:
            self.hips.append(hip.copy())
        else:
            self.hips.clear()
        if not np.isnan(tilt):
            self.tilts.append(tilt)
        if ank is not None:
            self.ankles.append(ank.copy())

        if box is not None:
            x1, y1, x2, y2 = box
            c = np.array([(x1 + x2) * 0.5, (y1 + y2) * 0.5], dtype=float)
            bh = max(1.0, y2 - y1)
            self.centers.append(c)
            self.box_hs.append(bh)

        # Fall first (overrides all)
        fall = self._fall_suspected()
        if fall:
            st = ActivityState("fall_suspected", 0.85, fall)
            self._last = st
            return st

        # Walking before EMA so motion is not smoothed into standing
        if (not upper_only) and frame.label not in ("lying",) and self._is_walking():
            st = ActivityState("walking", 0.78, "centroid_velocity")
            self.labels[-1] = "walking"
            self._ema["walking"] = self._ema.get("walking", 0.0) + 0.4
            self._last = st
            return st

        if upper_only and frame.label in ("standing", "sitting", "unknown"):
            self._last = frame
            return frame

        # EMA over recent labels (standing / sitting / lying)
        st = self._smooth(frame)
        self._last = st
        return st

    def _smooth(self, frame: ActivityState) -> ActivityState:
        # decay and boost current (walking handled only by _is_walking)
        for k in list(self._ema.keys()):
            self._ema[k] *= 0.85
        if frame.label != "walking":
            self._ema[frame.label] = self._ema.get(frame.label, 0.0) + 0.35
        if self.labels:
            vals, counts = np.unique(np.array(self.labels), return_counts=True)
            for v, c in zip(vals, counts):
                lab = str(v)
                if lab == "walking":
                    continue
                self._ema[lab] = self._ema.get(lab, 0.0) + 0.05 * float(c)

        candidates = {k: v for k, v in self._ema.items() if k != "walking"}
        if not candidates:
            return frame
        best = max(candidates.items(), key=lambda kv: kv[1])
        lab, score = best[0], best[1]
        conf = float(min(0.95, max(frame.confidence, score / 2.0)))
        detail = frame.detail if lab == frame.label else f"ema={lab}"
        return ActivityState(lab, conf, detail)

    def _is_walking(self) -> bool:
        """
        Walking = upright-ish + sustained body motion (any direction).
        Sideways OR toward/away camera both count (prior bug: horizontal-only).
        """
        if len(self.centers) < 5 or len(self.box_hs) < 5:
            self.walk_hits = max(0, self.walk_hits - 1)
            return False

        # Allow gait with slight knee bend (sitting flicker while stepping)
        mobile = sum(
            1 for x in self.labels if x in ("standing", "walking", "unknown")
        )
        lyingish = sum(1 for x in self.labels if x in ("lying", "fall_suspected"))
        if lyingish >= 3 or mobile < max(3, len(self.labels) // 3):
            self.walk_hits = max(0, self.walk_hits - 1)
            return False

        cs = list(self.centers)
        bhs = list(self.box_hs)
        hs = max(1.0, float(np.median(bhs)))
        # short + medium windows (sideways / diagonal)
        d_short = float(np.linalg.norm(cs[-1] - cs[-4])) / hs
        d_med = float(np.linalg.norm(cs[-1] - cs[max(0, len(cs) - 7)])) / hs

        # toward / away camera: bbox height changes while upright
        scale_ok = False
        if len(bhs) >= 6:
            rel = abs(bhs[-1] - bhs[-6]) / max(1.0, float(np.mean(bhs[-6:])))
            scale_ok = rel > 0.08  # ~8% size change over ~6 frames

        # hip path (more stable than box jitter)
        hip_ok = False
        if len(self.hips) >= 5:
            hips = list(self.hips)
            d_hip = float(np.linalg.norm(hips[-1] - hips[-5])) / hs
            hip_ok = d_hip > self.walk_speed * 3.5

        # ankle stride (legs moving even if torso slow)
        ank_ok = False
        if len(self.ankles) >= 5:
            anks = list(self.ankles)
            d_ank = float(np.linalg.norm(anks[-1] - anks[-5])) / hs
            ank_ok = d_ank > self.walk_speed * 4.0

        moving = (
            d_short > self.walk_speed * 3.0
            or d_med > self.walk_speed * 5.0
            or scale_ok
            or hip_ok
            or ank_ok
        )
        if moving:
            self.walk_hits += 1
        else:
            self.walk_hits = max(0, self.walk_hits - 1)

        return self.walk_hits >= self.walk_confirm

    def _fall_suspected(self) -> str:
        need = self.fall_frames * 2
        if len(self.tilts) < need:
            return ""
        tilts = list(self.tilts)
        past = float(np.mean(tilts[-need : -self.fall_frames]))
        now = float(np.mean(tilts[-max(2, self.fall_frames // 3) :]))
        dy = 0.0
        if len(self.hips) >= need:
            hips = list(self.hips)
            dy = float(hips[-1][1] - hips[-need][1])
        if past < 40 and now > 55 and dy > 12:
            return f"tilt {past:.0f}->{now:.0f} dy={dy:.0f}"
        if past < 35 and now > 60:
            return f"tilt {past:.0f}->{now:.0f}"
        # also: recent labels flipped to lying quickly
        if len(self.labels) >= 10:
            early = list(self.labels)[-10:-5]
            late = list(self.labels)[-5:]
            if (
                sum(1 for x in early if x in ("standing", "walking", "sitting")) >= 3
                and sum(1 for x in late if x == "lying") >= 3
            ):
                return "label standing/sit → lying"
        return ""
