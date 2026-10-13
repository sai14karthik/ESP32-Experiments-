"""Synthetic checks for literature-aligned activity rules."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity import ActivityTracker, classify_frame
from patients import PersonView, infer_role, summarize


def _blank() -> np.ndarray:
    k = np.zeros((17, 3), dtype=float)
    k[:, 2] = 0.9
    return k


def standing_kpts() -> np.ndarray:
    k = _blank()
    k[5], k[6] = [200, 120, 0.9], [280, 120, 0.9]
    k[11], k[12] = [210, 260, 0.9], [270, 260, 0.9]
    k[13], k[14] = [215, 360, 0.9], [265, 360, 0.9]
    k[15], k[16] = [220, 460, 0.9], [260, 460, 0.9]
    return k


def sitting_kpts() -> np.ndarray:
    k = _blank()
    k[5], k[6] = [200, 140, 0.9], [280, 140, 0.9]
    k[11], k[12] = [210, 280, 0.9], [270, 280, 0.9]
    k[13], k[14] = [180, 340, 0.9], [300, 340, 0.9]
    k[15], k[16] = [210, 300, 0.9], [270, 300, 0.9]
    return k


def lying_kpts() -> np.ndarray:
    k = _blank()
    k[5], k[6] = [120, 200, 0.9], [120, 260, 0.9]
    k[11], k[12] = [320, 205, 0.9], [320, 255, 0.9]
    k[13], k[14] = [400, 210, 0.9], [400, 250, 0.9]
    k[15], k[16] = [480, 215, 0.9], [480, 245, 0.9]
    return k


def upper_only_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [240, 80, 0.9]
    k[5], k[6] = [200, 140, 0.9], [280, 140, 0.9]
    for i in (11, 12, 13, 14, 15, 16):
        k[i] = [0, 0, 0.0]
    return k


def test_classify() -> None:
    s = classify_frame(standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280))
    assert s.label == "standing", s
    sit = classify_frame(sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280))
    assert sit.label == "sitting", sit
    # wide bbox → lying
    ly_box = classify_frame(
        standing_kpts(), box=(50, 300, 500, 420), frame_hw=(720, 1280)
    )
    assert ly_box.label == "lying", ly_box
    ly = classify_frame(lying_kpts(), box=(100, 180, 500, 280), frame_hw=(720, 1280))
    assert ly.label == "lying", ly
    up = classify_frame(
        upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
    )
    assert up.label == "standing", up
    print("classify_frame OK:", s.label, sit.label, ly_box.label, ly.label, up.label)


def test_fall() -> None:
    t = ActivityTracker(window=18, fall_frames=8)
    for _ in range(16):
        t.update(standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280))
    st = None
    for i in range(10):
        k = lying_kpts()
        k[:, 1] += 50 + i * 8
        st = t.update(k, box=(50, 300, 500, 420), frame_hw=(720, 1280))
    assert st is not None and st.label == "fall_suspected", st
    print("fall_suspected OK:", st.detail)


def test_scene() -> None:
    t1, t2 = ActivityTracker(), ActivityTracker()
    a1 = t1.update(lying_kpts(), box=(50, 300, 500, 420), frame_hw=(720, 1280))
    a2 = t2.update(standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280))
    people = [
        PersonView(1, a1, infer_role(a1, primary=True), (0, 0, 100, 80), 8000),
        PersonView(2, a2, infer_role(a2, primary=False), (0, 0, 50, 120), 6000),
    ]
    sc = summarize(people)
    assert sc.people_count == 2
    assert sc.patients >= 1
    print(
        "scene OK:",
        f"people={sc.people_count} patients={sc.patients}",
        [(p.track_id, p.role, p.activity.label) for p in sc.people],
    )


if __name__ == "__main__":
    test_classify()
    test_fall()
    test_scene()
    print("ALL_UNIT_OK")
