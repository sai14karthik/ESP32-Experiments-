"""Synthetic checks for all healthcare activities including walking."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity import ALL_ACTIVITIES, ActivityTracker, classify_frame
from patients import PersonView, infer_role, summarize


def _blank() -> np.ndarray:
    k = np.zeros((17, 3), dtype=float)
    # conf=0 until a joint is set (avoids draw lines to origin)
    return k


def standing_kpts(dx: float = 0.0) -> np.ndarray:
    k = _blank()
    k[0] = [240 + dx, 80, 0.9]
    k[5], k[6] = [200 + dx, 120, 0.9], [280 + dx, 120, 0.9]
    k[11], k[12] = [210 + dx, 260, 0.9], [270 + dx, 260, 0.9]
    k[13], k[14] = [215 + dx, 360, 0.9], [265 + dx, 360, 0.9]
    k[15], k[16] = [220 + dx, 460, 0.9], [260 + dx, 460, 0.9]
    return k


def sitting_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [240, 100, 0.9]
    k[5], k[6] = [200, 140, 0.9], [280, 140, 0.9]
    k[11], k[12] = [210, 280, 0.9], [270, 280, 0.9]
    k[13], k[14] = [180, 340, 0.9], [300, 340, 0.9]
    k[15], k[16] = [210, 300, 0.9], [270, 300, 0.9]
    return k


def lying_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [80, 230, 0.9]
    k[5], k[6] = [120, 200, 0.9], [120, 260, 0.9]
    k[11], k[12] = [320, 205, 0.9], [320, 255, 0.9]
    k[13], k[14] = [400, 210, 0.9], [400, 250, 0.9]
    k[15], k[16] = [480, 215, 0.9], [480, 245, 0.9]
    return k


def upper_only_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [240, 80, 0.9]
    k[5], k[6] = [200, 140, 0.9], [280, 140, 0.9]
    return k


def overhead_sitting_kpts() -> np.ndarray:
    """Ceiling cam sit: bent knees, ankles near hips."""
    k = _blank()
    k[0] = [320, 180, 0.9]
    k[5], k[6] = [280, 220, 0.9], [360, 220, 0.9]
    k[11], k[12] = [290, 300, 0.9], [350, 300, 0.9]
    k[13], k[14] = [270, 340, 0.9], [370, 340, 0.9]
    k[15], k[16] = [285, 320, 0.9], [355, 320, 0.9]
    return k


def overhead_lying_kpts() -> np.ndarray:
    """Ceiling cam prone: shoulders wide vs short body height."""
    k = _blank()
    k[0] = [320, 200, 0.9]
    k[5], k[6] = [250, 240, 0.9], [390, 240, 0.9]  # wide shoulders
    k[11], k[12] = [270, 300, 0.9], [370, 300, 0.9]
    k[13], k[14] = [280, 330, 0.9], [360, 330, 0.9]
    k[15], k[16] = [290, 350, 0.9], [350, 350, 0.9]
    return k


def test_classify() -> None:
    s = classify_frame(standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280))
    assert s.label == "standing", s
    sit = classify_frame(sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280))
    assert sit.label == "sitting", sit
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
    oh_sit = classify_frame(
        overhead_sitting_kpts(), box=(250, 160, 400, 370), frame_hw=(480, 640)
    )
    assert oh_sit.label == "sitting", oh_sit
    oh_lie = classify_frame(
        overhead_lying_kpts(), box=(240, 190, 400, 360), frame_hw=(480, 640)
    )
    assert oh_lie.label == "lying", oh_lie
    print(
        "classify_frame OK:",
        s.label,
        sit.label,
        ly_box.label,
        ly.label,
        up.label,
        oh_sit.label,
        oh_lie.label,
    )


def test_walking() -> None:
    """Sideways walk + toward-camera walk must both register as walking."""
    t = ActivityTracker(window=20, walk_speed=0.022, walk_confirm=2)
    st = None
    for i in range(14):
        dx = i * 28.0  # ~person-height fraction per few frames
        box = (180 + dx, 80, 300 + dx, 500)
        st = t.update(standing_kpts(dx=dx), box=box, frame_hw=(720, 1280))
    assert st is not None and st.label == "walking", st
    print("walking sideways OK:", st.detail)

    t2 = ActivityTracker(window=20, walk_speed=0.022, walk_confirm=2)
    st2 = None
    for i in range(14):
        # toward camera: box grows + hip moves down slightly (not sideways)
        scale = 1.0 + i * 0.04
        cx, cy = 240, 290
        half_w, half_h = 60 * scale, 210 * scale
        box = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
        k = standing_kpts(dx=0)
        k[:, 1] += i * 6  # slight vertical centroid shift
        st2 = t2.update(k, box=box, frame_hw=(720, 1280))
    assert st2 is not None and st2.label == "walking", st2
    print("walking toward-camera OK:", st2.detail)


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


def test_all_labels_covered() -> None:
    assert "walking" in ALL_ACTIVITIES
    assert set(ALL_ACTIVITIES) == {
        "standing",
        "sitting",
        "walking",
        "lying",
        "fall_suspected",
        "unknown",
    }
    print("ALL_ACTIVITIES OK:", ALL_ACTIVITIES)


def test_scene() -> None:
    from activity import ActivityState

    t1, t2 = ActivityTracker(), ActivityTracker()
    a1 = t1.update(lying_kpts(), box=(50, 300, 500, 420), frame_hw=(720, 1280))
    a2 = t2.update(standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280))
    a3 = ActivityState("walking", 0.8, "forced")
    people = [
        PersonView(1, a1, infer_role(a1, primary=True), (0, 0, 100, 80), 8000),
        PersonView(2, a2, infer_role(a2, primary=False), (0, 0, 50, 120), 6000),
        PersonView(3, a3, infer_role(a3, primary=False), (0, 0, 60, 140), 7000),
    ]
    sc = summarize(people)
    assert sc.people_count == 3
    assert sc.patients >= 1
    assert any(p.activity.label == "walking" for p in sc.people)
    print(
        "scene OK:",
        f"people={sc.people_count} patients={sc.patients}",
        [(p.track_id, p.role, p.activity.label) for p in sc.people],
    )


if __name__ == "__main__":
    test_all_labels_covered()
    test_classify()
    test_walking()
    test_fall()
    test_scene()
    print("ALL_UNIT_OK")
