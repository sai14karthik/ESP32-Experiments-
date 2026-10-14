"""
Healthcare activity from YOLO-pose COCO-17 keypoints.

Works for side views and overhead / ceiling cameras (Sense room cams):
  - lying: bbox aspect, torso tilt, OR body width/height from keypoints (top-down)
  - sitting: knee/hip angles OR ankles close to hips (foreshortened sit)
  - standing: tall body vs shoulder width + straight legs
  - walking / fall_suspected: temporal (unchanged)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Deque, Optional

import numpy as np

NOSE, L_SHO, R_SHO, L_HIP, R_HIP = 0, 5, 6, 11, 12
L_KNEE, R_KNEE, L_ANK, R_ANK = 13, 14, 15, 16

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


def _ok(kpts: np.ndarray, idx: int, thr: float = 0.35) -> bool:
    if kpts is None or idx >= len(kpts):
        return False
    if kpts.shape[1] >= 3:
        return float(kpts[idx, 2]) >= thr
    return True


def _pt(kpts: np.ndarray, idx: int, thr: float = 0.35) -> Optional[np.ndarray]:
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
    """0° ≈ image-vertical, 90° ≈ image-horizontal."""
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


def _kpt_span(kpts: np.ndarray, thr: float = 0.3) -> tuple[float, float, float]:
    """Return (width, height, shoulder_width) of confident keypoints."""
    pts = []
    for i in range(min(17, len(kpts))):
        if _ok(kpts, i, thr=thr):
            pts.append(kpts[i, :2].astype(float))
    if len(pts) < 3:
        return float("nan"), float("nan"), float("nan")
    arr = np.stack(pts, axis=0)
    w = float(arr[:, 0].max() - arr[:, 0].min())
    h = float(arr[:, 1].max() - arr[:, 1].min())
    l_sho, r_sho = _pt(kpts, L_SHO, thr), _pt(kpts, R_SHO, thr)
    sw = float(np.linalg.norm(l_sho - r_sho)) if l_sho is not None and r_sho is not None else float("nan")
    return w, h, sw


def classify_frame(
    kpts: np.ndarray,
    box: Optional[tuple[float, float, float, float]] = None,
    frame_hw: Optional[tuple[int, int]] = None,
) -> ActivityState:
    """
    Priority: lying → sitting → standing.
    Overhead-safe: uses body width/height + ankle–hip compactness, not only side-view tilt.
    """
    if kpts is None or len(kpts) < 17:
        return ActivityState("unknown", 0.0, "no_pose")

    aspect = float("nan")
    if box is not None:
        x1, y1, x2, y2 = box
        bw = max(1.0, x2 - x1)
        bh = max(1.0, y2 - y1)
        aspect = bw / bh

    nose = _pt(kpts, NOSE)
    l_sho, r_sho = _pt(kpts, L_SHO), _pt(kpts, R_SHO)
    l_hip, r_hip = _pt(kpts, L_HIP), _pt(kpts, R_HIP)
    l_knee, r_knee = _pt(kpts, L_KNEE), _pt(kpts, R_KNEE)
    l_ank, r_ank = _pt(kpts, L_ANK), _pt(kpts, R_ANK)

    shoulder = _mid(l_sho, r_sho)
    hip = _mid(l_hip, r_hip)
    ankle = _mid(l_ank, r_ank)
    knee = _mid(l_knee, r_knee)

    span_w, span_h, sho_w = _kpt_span(kpts)
    body_wh = (
        span_w / max(span_h, 1.0) if not np.isnan(span_w) and not np.isnan(span_h) else float("nan")
    )
    # Overhead lying: body fills frame more like a blob — wide vs tall OR shoulders wide vs height
    flatness = (
        sho_w / max(span_h, 1.0) if not np.isnan(sho_w) and not np.isnan(span_h) else float("nan")
    )

    if shoulder is not None and hip is None:
        return ActivityState("unknown", 0.35, "upper_body_only (need legs for posture)")

    if shoulder is None or hip is None:
        return ActivityState("unknown", 0.0, "missing_torso")

    tilt = _torso_tilt_deg(shoulder, hip)
    torso_len = float(np.linalg.norm(shoulder - hip))

    # --- Lying ---
    box_flat = not np.isnan(aspect) and aspect >= 1.35
    pose_flat = not np.isnan(body_wh) and body_wh >= 0.9
    if box_flat and (
        (not np.isnan(tilt) and tilt >= 35)
        or (not np.isnan(body_wh) and body_wh >= 1.0)
        or (not np.isnan(flatness) and flatness >= 0.55)
    ):
        return ActivityState("lying", 0.82, f"bbox_aspect={aspect:.2f} pose corroborates")
    if not np.isnan(tilt) and tilt >= 60 and pose_flat:
        return ActivityState("lying", 0.82, f"torso_tilt={tilt:.0f} kpt_aspect={body_wh:.2f}")
    if (
        not np.isnan(body_wh)
        and body_wh >= 1.35
        and (not np.isnan(tilt) and tilt >= 30 or box_flat)
    ):
        return ActivityState("lying", 0.82, f"kpt_aspect={body_wh:.2f}")
    if not np.isnan(flatness) and flatness >= 0.65 and (
        (not np.isnan(tilt) and tilt >= 25)
        or (not np.isnan(body_wh) and body_wh >= 0.9)
    ):
        # Top-down on bed: shoulders almost as wide as body height in image
        return ActivityState("lying", 0.8, f"flatness={flatness:.2f} tilt={tilt:.0f}")
    # Prone along image vertical (common overhead fail): ankles near shoulders in Y
    # and torso short vs shoulder width → collapsed on bed
    if (
        ankle is not None
        and not np.isnan(sho_w)
        and torso_len > 1
        and sho_w / torso_len >= 1.15
    ):
        ank_hip = float(np.linalg.norm(ankle - hip))
        if ank_hip < torso_len * 1.35:
            return ActivityState(
                "lying", 0.78, f"prone_compact sho/torso={sho_w / torso_len:.2f}"
            )

    # --- Angles ---
    knee_angles = []
    for hi, kn, an in ((l_hip, l_knee, l_ank), (r_hip, r_knee, r_ank)):
        if hi is not None and kn is not None and an is not None:
            knee_angles.append(_angle_deg(hi, kn, an))
    knee_ang = _mean_angle(knee_angles)

    hip_angles = []
    for sh, hi, kn in ((l_sho, l_hip, l_knee), (r_sho, r_hip, r_knee)):
        if sh is not None and hi is not None and kn is not None:
            hip_angles.append(_angle_deg(sh, hi, kn))
    hip_ang = _mean_angle(hip_angles)

    nhk = float("nan")
    if nose is not None and hip is not None:
        for kn in (l_knee, r_knee):
            if kn is not None:
                a = _angle_deg(nose, hip, kn)
                if not np.isnan(a):
                    nhk = a if np.isnan(nhk) else 0.5 * (nhk + a)

    # Ankle–hip distance vs torso (sitting / crouched from above)
    sit_compact = float("nan")
    if ankle is not None and torso_len > 1:
        sit_compact = float(np.linalg.norm(ankle - hip)) / torso_len
    elif knee is not None and torso_len > 1:
        sit_compact = float(np.linalg.norm(knee - hip)) / torso_len

    # --- Sitting ---
    # Angle: bent knees/hips. Geom: ankles close to hips (overhead sit); stand ~1.4+
    sit_angle = (not np.isnan(knee_ang) and knee_ang < 135) or (
        not np.isnan(hip_ang) and hip_ang < 120
    )
    # Short projected legs alone are not enough to call someone seated: an
    # elevated camera can foreshorten a standing person's hip-to-ankle span.
    # Keep the compactness cue for overhead/occluded views unless visible knees
    # are clearly straight.
    knees_straight = not np.isnan(knee_ang) and knee_ang >= 150
    sit_geom = (
        not np.isnan(sit_compact)
        and sit_compact < 1.15
        and not knees_straight
    )
    if sit_angle or sit_geom:
        conf = 0.72
        if not np.isnan(knee_ang) and knee_ang < 120:
            conf = 0.88
        elif sit_geom:
            conf = 0.8
        return ActivityState(
            "sitting",
            conf,
            f"knee={knee_ang:.0f} hip_ang={hip_ang:.0f} compact={sit_compact:.2f}",
        )
    if not np.isnan(nhk) and nhk < 115:
        return ActivityState("sitting", 0.72, f"nose_hip_knee={nhk:.0f}")

    # --- Standing ---
    tall = (not np.isnan(flatness) and flatness < 0.42) or (
        not np.isnan(aspect) and aspect < 0.75
    )
    standing_leg_evidence = not np.isnan(knee_ang) and knee_ang >= 140
    legs_long = (
        np.isnan(sit_compact)
        or sit_compact >= 1.25
        or knees_straight
    )
    straight = np.isnan(knee_ang) or knee_ang >= 140
    upright_tilt = np.isnan(tilt) or tilt < 45

    if tall and standing_leg_evidence and legs_long and straight and upright_tilt:
        conf = 0.8 if (not np.isnan(knee_ang) and knee_ang >= 155) else 0.65
        return ActivityState(
            "standing",
            conf,
            f"tilt={tilt:.0f} knee={knee_ang:.0f} flat={flatness:.2f} compact={sit_compact:.2f}",
        )

    if (
        standing_leg_evidence
        and upright_tilt
        and legs_long
        and (np.isnan(flatness) or flatness < 0.5)
    ):
        return ActivityState("standing", 0.5, f"upright_fallback tilt={tilt:.0f}")

    return ActivityState(
        "unknown",
        0.3,
        f"tilt={tilt:.0f} knee={knee_ang:.0f} flat={flatness:.2f} compact={sit_compact:.2f}",
    )


class ActivityTracker:
    """Temporal smoothing + walking / fall_suspected."""

    def __init__(
        self,
        window: int = 20,
        walk_speed: float = 0.022,
        walk_confirm: int = 2,
    ):
        self.window = window
        self.walk_speed = walk_speed
        self._fall_active = False
        self._fall_reason = ""
        self._upright_since: Optional[float] = None
        self.walk_confirm = walk_confirm
        self.labels: Deque[str] = deque(maxlen=window)
        self.hips: Deque[np.ndarray] = deque(maxlen=window)
        self.hip_times: Deque[float] = deque(maxlen=window)
        self.centers: Deque[np.ndarray] = deque(maxlen=12)
        self.center_times: Deque[float] = deque(maxlen=12)
        self.box_hs: Deque[float] = deque(maxlen=12)
        self.ankles: Deque[np.ndarray] = deque(maxlen=12)
        self.ankle_times: Deque[float] = deque(maxlen=12)
        self.fall_observations: Deque[
            tuple[float, str, float, float, float, float, float]
        ] = deque(maxlen=64)
        self._last_update_time: Optional[float] = None
        self.walk_hits = 0
        self._ema: dict[str, float] = {}
        self._last = ActivityState()

    def update(
        self,
        kpts: np.ndarray,
        box: Optional[tuple[float, float, float, float]] = None,
        frame_hw: Optional[tuple[int, int]] = None,
        timestamp: Optional[float] = None,
    ) -> ActivityState:
        now = time.monotonic() if timestamp is None else float(timestamp)
        if self._last_update_time is not None and now - self._last_update_time > 1.25:
            self._reset_temporal_history()
        self._last_update_time = now

        frame = classify_frame(kpts, box=box, frame_hw=frame_hw)
        hip = _mid(_pt(kpts, L_HIP, 0.3), _pt(kpts, R_HIP, 0.3))
        sho = _mid(_pt(kpts, L_SHO), _pt(kpts, R_SHO))
        ank = _mid(_pt(kpts, L_ANK, 0.25), _pt(kpts, R_ANK, 0.25))
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
            self.hip_times.append(now)
        else:
            self.hips.clear()
            self.hip_times.clear()
        if ank is not None:
            self.ankles.append(ank.copy())
            self.ankle_times.append(now)
        else:
            self.ankles.clear()
            self.ankle_times.clear()

        if box is not None:
            x1, y1, x2, y2 = box
            c = np.array([(x1 + x2) * 0.5, (y1 + y2) * 0.5], dtype=float)
            bh = max(1.0, y2 - y1)
            self.centers.append(c)
            self.center_times.append(now)
            self.box_hs.append(bh)

            if (
                hip is not None
                and sho is not None
                and not np.isnan(tilt)
                and frame_hw is not None
                and frame_hw[0] > 0
                and frame_hw[1] > 0
            ):
                if (
                    self.fall_observations
                    and now - self.fall_observations[-1][0] > 1.25
                ):
                    self.fall_observations.clear()
                fh = float(frame_hw[0])
                self.fall_observations.append(
                    (
                        now,
                        frame.label,
                        tilt,
                        c[0] / float(frame_hw[1]),
                        c[1] / fh,
                        bh / fh,
                        (x2 - x1) / bh,
                    )
                )
        elif (
            self.fall_observations
            and now - self.fall_observations[-1][0] > 1.25
        ):
            self.fall_observations.clear()

        fall = self._fall_suspected(now)
        if fall:
            self._fall_active = True
            self._fall_reason = fall

        if self._fall_active:
            upright_now = (
                frame.label in ("standing", "walking")
                and not np.isnan(tilt)
                and tilt <= 35
            )
            if upright_now:
                if self._upright_since is None:
                    self._upright_since = now
                elif now - self._upright_since >= 0.75:
                    self._fall_active = False
                    self._fall_reason = ""
                    self._upright_since = None
            else:
                self._upright_since = None

        if self._fall_active:
            st = ActivityState(
                "fall_suspected", 0.9, self._fall_reason or "remain low after fall-like motion"
            )
            self._last = st
            return st

        if (not upper_only) and frame.label not in ("lying",) and self._is_walking():
            st = ActivityState("walking", 0.78, "centroid_velocity")
            self.labels[-1] = "walking"
            self._ema["walking"] = self._ema.get("walking", 0.0) + 0.4
            self._last = st
            return st

        st = self._smooth(frame)
        self._last = st
        return st

    def _reset_temporal_history(self) -> None:
        """Do not compare motion/posture samples across a long camera gap."""
        self.labels.clear()
        self.hips.clear()
        self.hip_times.clear()
        self.centers.clear()
        self.center_times.clear()
        self.box_hs.clear()
        self.ankles.clear()
        self.ankle_times.clear()
        self.fall_observations.clear()
        self.walk_hits = 0
        self._ema.clear()
        self._upright_since = None

    def _smooth(self, frame: ActivityState) -> ActivityState:
        for k in list(self._ema.keys()):
            self._ema[k] *= 0.78  # faster forget — posture changes quickly
        boost = 0.55 if frame.label in ("lying", "sitting", "fall_suspected") else 0.35
        if frame.confidence >= 0.75:
            boost += 0.15
        if frame.label != "walking":
            self._ema[frame.label] = self._ema.get(frame.label, 0.0) + boost
        # Strong lie/sit should not stay stuck on standing
        if frame.label in ("lying", "sitting") and frame.confidence >= 0.75:
            self._ema["standing"] = self._ema.get("standing", 0.0) * 0.4

        candidates = {k: v for k, v in self._ema.items() if k != "walking"}
        if not candidates:
            return frame
        best = max(candidates.items(), key=lambda kv: kv[1])
        lab, score = best[0], best[1]
        conf = float(min(0.95, max(frame.confidence, score / 2.0)))
        detail = frame.detail if lab == frame.label else f"ema={lab}"
        return ActivityState(lab, conf, detail)

    def _is_walking(self) -> bool:
        if len(self.centers) < 3 or len(self.box_hs) < 3:
            self.walk_hits = max(0, self.walk_hits - 1)
            return False

        mobile = sum(
            1 for x in self.labels if x in ("standing", "walking", "unknown")
        )
        lyingish = sum(1 for x in self.labels if x in ("lying", "fall_suspected", "sitting"))
        if lyingish >= 4 or mobile < max(3, len(self.labels) // 3):
            self.walk_hits = max(0, self.walk_hits - 1)
            return False

        cs = list(self.centers)
        ts = list(self.center_times)
        bhs = list(self.box_hs)
        hs = max(1.0, float(np.median(bhs)))

        def normalized_rate(points: list[np.ndarray], times: list[float], lookback: float) -> float:
            if len(points) < 2 or len(points) != len(times):
                return 0.0
            now = times[-1]
            candidates = [
                j
                for j in range(len(times) - 1)
                if lookback * 0.65 <= now - times[j] <= lookback * 1.8
            ]
            if not candidates:
                return 0.0
            j = min(candidates, key=lambda index: abs((now - times[index]) - lookback))
            elapsed = max(1e-3, now - times[j])
            return float(np.linalg.norm(points[-1] - points[j])) / hs / elapsed

        short_rate = normalized_rate(cs, ts, 0.35)
        medium_rate = normalized_rate(cs, ts, 0.7)

        scale_ok = False
        prior = [
            j
            for j in range(len(ts) - 1)
            if 0.4 <= ts[-1] - ts[j] <= 1.0
        ]
        if prior:
            j = min(prior, key=lambda index: abs((ts[-1] - ts[index]) - 0.65))
            elapsed = max(1e-3, ts[-1] - ts[j])
            rel = abs(bhs[-1] - bhs[j]) / max(1.0, (bhs[-1] + bhs[j]) * 0.5)
            scale_ok = rel / elapsed > 0.14

        hip_rate = normalized_rate(list(self.hips), list(self.hip_times), 0.45)
        ankle_rate = normalized_rate(list(self.ankles), list(self.ankle_times), 0.45)

        moving = (
            short_rate > self.walk_speed * 8.0
            or medium_rate > self.walk_speed * 7.0
            or scale_ok
            or hip_rate > self.walk_speed * 7.0
            or ankle_rate > self.walk_speed * 8.0
        )
        if moving:
            self.walk_hits += 1
        else:
            self.walk_hits = max(0, self.walk_hits - 1)

        return self.walk_hits >= self.walk_confirm

    def _fall_suspected(self, now: float) -> str:
        """Conservative fall-like transition: upright → sustained low posture + motion."""
        observations = [
            obs for obs in self.fall_observations if now - obs[0] <= 3.0
        ]
        if len(observations) < 4:
            return ""

        recent = [
            obs
            for obs in observations
            if now - obs[0] <= 0.8 and obs[1] == "lying"
        ]
        if (
            len(recent) < 3
            or recent[-1][0] - recent[0][0] < 0.25
            or recent[-1][1] != "lying"
        ):
            return ""

        lie_start = recent[0][0]
        upright = [
            obs
            for obs in observations
            if 0.35 <= lie_start - obs[0] <= 2.5
            and obs[1] in ("standing", "walking")
        ]
        if not upright:
            return ""

        before = upright[-1]
        if any(
            obs[1] == "sitting" and before[0] < obs[0] < lie_start
            for obs in observations
        ):
            return ""

        current = recent[-1]
        motion_ratio = float(
            np.hypot(current[3] - before[3], current[4] - before[4])
            / max(before[5], 1e-3)
        )
        aspect_change = current[6] - before[6]
        # A posture label change alone is not a fall. Require visible movement
        # in either image direction; a flatter box supports smaller movement.
        if motion_ratio >= 0.12 or (motion_ratio >= 0.05 and aspect_change >= 0.45):
            return f"upright→low posture; motion={motion_ratio:.2f}h"
        return ""
