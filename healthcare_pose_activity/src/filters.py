"""Drop false person boxes (hands/arms) and draw COCO-17 pose correctly."""
from __future__ import annotations

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
    min_kpts: int = 4,
    min_area_frac: float = 0.025,
    min_vs_largest: float = 0.28,
) -> list[int]:
    """
    Keep real people; drop hand/arm fragments that YOLO sometimes labels person.
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
        if max_a > 0 and a < min_vs_largest * max_a:
            continue
        bw = float(boxes[i, 2] - boxes[i, 0])
        bh = float(boxes[i, 3] - boxes[i, 1])
        if bh < 1 or bw < 1:
            continue
        aspect = bw / bh
        if aspect > 2.0 and a < 0.55 * max_a:
            continue
        n_kpt, has_face = keypoint_score(kdata[i])
        if n_kpt < min_kpts or not has_face:
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
        if c < thr:
            pts.append(None)
        else:
            pts.append((int(kpts[j, 0]), int(kpts[j, 1])))

    for a, b in SKELETON:
        if pts[a] is not None and pts[b] is not None:
            cv2.line(frame, pts[a], pts[b], color, 2, lineType=cv2.LINE_AA)

    for j, p in enumerate(pts):
        if p is not None:
            cv2.circle(frame, p, 4, KPT_COLOR[j % len(KPT_COLOR)], -1, lineType=cv2.LINE_AA)
            cv2.circle(frame, p, 4, (255, 255, 255), 1, lineType=cv2.LINE_AA)
