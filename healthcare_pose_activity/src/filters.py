"""Drop false person boxes (hands/arms) and draw COCO-17 pose correctly."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Official Ultralytics / COCO-17 skeleton (0-indexed).
# Matches ultralytics Annotator (1-indexed list minus 1).
SKELETON = [
    (15, 13),
    (13, 11),
    (16, 14),
    (14, 12),
    (11, 12),
    (5, 11),
    (6, 12),
    (5, 6),
    (5, 7),
    (6, 8),
    (7, 9),
    (8, 10),
    (1, 2),
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),
    (3, 5),
    (4, 6),
]

# Soft palette similar to Ultralytics pose colors (BGR)
KPT_COLOR = [
    (0, 255, 0),
    (0, 255, 0),
    (0, 255, 0),
    (0, 255, 0),
    (0, 255, 0),
    (255, 128, 0),
    (255, 128, 0),
    (255, 128, 0),
    (255, 128, 0),
    (255, 128, 0),
    (255, 128, 0),
    (51, 153, 255),
    (51, 153, 255),
    (51, 153, 255),
    (51, 153, 255),
    (51, 153, 255),
    (51, 153, 255),
]


@dataclass
class _IdentityTrack:
    box: np.ndarray
    last_frame: int
    raw_id: int | None


class StablePersonIds:
    """Keep session-local display IDs through brief tracker-ID/detection gaps."""

    def __init__(self, max_age: int = 90, min_iou: float = 0.15):
        self.max_age = max_age
        self.min_iou = min_iou
        self.frame = 0
        self.next_id = 1
        self.raw_to_stable: dict[int, int] = {}
        self.tracks: dict[int, _IdentityTrack] = {}

    @staticmethod
    def _iou(a: np.ndarray, b: np.ndarray) -> float:
        x1 = max(float(a[0]), float(b[0]))
        y1 = max(float(a[1]), float(b[1]))
        x2 = min(float(a[2]), float(b[2]))
        y2 = min(float(a[3]), float(b[3]))
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        area_a = max(0.0, float(a[2] - a[0])) * max(0.0, float(a[3] - a[1]))
        area_b = max(0.0, float(b[2] - b[0])) * max(0.0, float(b[3] - b[1]))
        union = area_a + area_b - intersection
        return intersection / union if union > 0 else 0.0

    def update(
        self,
        boxes: np.ndarray,
        raw_ids: list[int | None] | None = None,
    ) -> list[int]:
        """Return stable IDs in detection order; raw IDs may be absent per frame."""
        self.frame += 1
        boxes = np.asarray(boxes, dtype=float).reshape((-1, 4))
        if raw_ids is None:
            raw_ids = [None] * len(boxes)
        if len(raw_ids) != len(boxes):
            raise ValueError("raw_ids must have one entry per box")

        result: list[int | None] = [None] * len(boxes)
        used: set[int] = set()
        current_raw_ids = {raw for raw in raw_ids if raw is not None}

        # Preserve IDs supplied by the motion tracker whenever possible.
        for index, raw_id in enumerate(raw_ids):
            if raw_id is None:
                continue
            stable_id = self.raw_to_stable.get(raw_id)
            track = self.tracks.get(stable_id) if stable_id is not None else None
            if (
                track is not None
                and self.frame - track.last_frame <= self.max_age
                and stable_id not in used
            ):
                result[index] = stable_id
                used.add(stable_id)

        # Recover a short discontinuity by geometric overlap, not detection index.
        candidates: list[tuple[float, int, int]] = []
        for index, box in enumerate(boxes):
            if result[index] is not None:
                continue
            for stable_id, track in self.tracks.items():
                if stable_id in used or track.raw_id in current_raw_ids:
                    continue
                if self.frame - track.last_frame > self.max_age:
                    continue
                overlap = self._iou(box, track.box)
                if overlap >= self.min_iou:
                    candidates.append((overlap, index, stable_id))
        candidates.sort(reverse=True)
        for _overlap, index, stable_id in candidates:
            if result[index] is None and stable_id not in used:
                result[index] = stable_id
                used.add(stable_id)

        for index in range(len(boxes)):
            if result[index] is None:
                result[index] = self.next_id
                self.next_id += 1

        for index, (box, raw_id) in enumerate(zip(boxes, raw_ids)):
            stable_id = int(result[index])
            for previous_raw, previous_stable in list(self.raw_to_stable.items()):
                if previous_stable == stable_id and previous_raw != raw_id:
                    del self.raw_to_stable[previous_raw]
            self.tracks[stable_id] = _IdentityTrack(
                box=box.copy(), last_frame=self.frame, raw_id=raw_id
            )
            if raw_id is not None:
                self.raw_to_stable[raw_id] = stable_id

        expired = {
            stable_id
            for stable_id, track in self.tracks.items()
            if self.frame - track.last_frame > self.max_age
        }
        for stable_id in expired:
            del self.tracks[stable_id]
        self.raw_to_stable = {
            raw: stable
            for raw, stable in self.raw_to_stable.items()
            if stable in self.tracks
        }
        return [int(stable_id) for stable_id in result]


def keypoint_score(kpts: np.ndarray, thr: float = 0.30) -> tuple[int, bool]:
    """Return (#confident kpts, has face/shoulders)."""
    if kpts is None or len(kpts) < 17:
        return 0, False
    conf = kpts[:, 2] if kpts.shape[1] >= 3 else np.ones(len(kpts))
    n = int(np.sum(conf >= thr))
    has_face = bool(conf[0] >= thr or (conf[5] >= thr and conf[6] >= thr))
    return n, has_face


def filter_person_indices(
    kdata: np.ndarray,
    boxes: np.ndarray,
    frame_hw: tuple[int, int],
    *,
    min_kpts: int = 5,
    min_area_frac: float = 0.01,
) -> list[int]:
    """
    Keep plausible people using per-detection size/keypoints; drop tiny hand/arm
    fragments without rejecting a valid distant person because of a larger one.
    """
    if kdata is None or boxes is None or len(boxes) == 0:
        return []
    h, w = frame_hw
    frame_area = float(h * w)
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    max_a = float(np.max(areas)) if len(areas) else 0.0
    keep: list[int] = []
    for i in range(len(boxes)):
        a = float(areas[i])
        if a < min_area_frac * frame_area:
            continue
        bw = float(boxes[i, 2] - boxes[i, 0])
        bh = float(boxes[i, 3] - boxes[i, 1])
        if bh < 1 or bw < 1:
            continue
        aspect = bw / bh
        # hand-like: very wide and much smaller than main person
        if aspect > 2.2 and a < 0.50 * max_a:
            continue
        n_kpt, has_face = keypoint_score(kdata[i], thr=0.35)
        if n_kpt < min_kpts or not has_face:
            continue
        # Prefer detections with at least one hip (better activity)
        conf = kdata[i][:, 2] if kdata[i].shape[1] >= 3 else np.ones(17)
        has_hip = bool(conf[11] >= 0.3 or conf[12] >= 0.3)
        # Keep upper-body-only only if large in frame (close-up)
        if not has_hip and a < 0.12 * frame_area:
            continue
        keep.append(i)
    return keep


def draw_person(
    frame,
    box: np.ndarray,
    kpts: np.ndarray,
    color: tuple[int, int, int],
    thr: float = 0.5,
) -> None:
    """Draw box + COCO skeleton; only connect keypoints above confidence thr."""
    import cv2

    x1, y1, x2, y2 = map(int, box)
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    if kpts is None or len(kpts) < 17:
        return

    pts: list[tuple[int, int] | None] = []
    confs: list[float] = []
    for j in range(17):
        c = float(kpts[j, 2]) if kpts.shape[1] >= 3 else 1.0
        confs.append(c)
        x, y = float(kpts[j, 0]), float(kpts[j, 1])
        # Skip low-conf or unset (0,0) joints — avoids lines to image origin
        if c < thr or (x < 1.0 and y < 1.0):
            pts.append(None)
        else:
            pts.append((int(x), int(y)))

    for a, b in SKELETON:
        if pts[a] is not None and pts[b] is not None:
            cv2.line(frame, pts[a], pts[b], color, 2, lineType=cv2.LINE_AA)

    for j, p in enumerate(pts):
        if p is not None:
            cv2.circle(frame, p, 4, KPT_COLOR[j % len(KPT_COLOR)], -1, lineType=cv2.LINE_AA)
            cv2.circle(frame, p, 4, (255, 255, 255), 1, lineType=cv2.LINE_AA)
