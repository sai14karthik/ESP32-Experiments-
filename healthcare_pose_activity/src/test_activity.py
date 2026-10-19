"""Synthetic checks for all healthcare activities including walking."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from activity import ALL_ACTIVITIES, ActivityTracker, classify_frame
from filters import StablePersonIds
from patients import PersonView, summarize


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


def foreshortened_standing_kpts() -> np.ndarray:
    """Upright standing pose with short projected legs from an elevated view."""
    k = standing_kpts()
    k[13], k[14] = [215, 330, 0.9], [265, 330, 0.9]
    k[15], k[16] = [220, 400, 0.9], [260, 400, 0.9]
    return k


def sitting_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [240, 100, 0.9]
    k[5], k[6] = [200, 140, 0.9], [280, 140, 0.9]
    k[11], k[12] = [210, 280, 0.9], [270, 280, 0.9]
    k[13], k[14] = [180, 340, 0.9], [300, 340, 0.9]
    k[15], k[16] = [210, 300, 0.9], [270, 300, 0.9]
    return k


def reclining_sitting_kpts() -> np.ndarray:
    """Seated with a sideways-leaning torso; legs still show a seated pose."""
    k = sitting_kpts()
    k[5], k[6] = [310, 250, 0.9], [350, 310, 0.9]
    return k


def lying_kpts() -> np.ndarray:
    k = _blank()
    k[0] = [80, 230, 0.9]
    k[5], k[6] = [120, 200, 0.9], [120, 260, 0.9]
    k[11], k[12] = [320, 205, 0.9], [320, 255, 0.9]
    k[13], k[14] = [400, 210, 0.9], [400, 250, 0.9]
    k[15], k[16] = [480, 215, 0.9], [480, 245, 0.9]
    return k


def bed_supine_kpts() -> np.ndarray:
    """Person lying on a bed, seen from the foot. Legs are under the blanket."""
    k = _blank()
    k[0] = [320, 180, 0.9]
    k[5], k[6] = [250, 240, 0.9], [390, 240, 0.9]
    k[11], k[12] = [270, 360, 0.9], [370, 360, 0.9]
    return k


def along_camera_lying_kpts() -> np.ndarray:
    """Lying along the camera axis. Torso is a bit longer than the shoulders; no legs."""
    k = _blank()
    k[0] = [320, 150, 0.9]
    k[5], k[6] = [250, 200, 0.9], [390, 200, 0.9]
    k[7], k[8] = [230, 280, 0.9], [410, 280, 0.9]
    k[11], k[12] = [290, 370, 0.9], [350, 370, 0.9]
    return k


def tall_silhouette_standing_kpts() -> np.ndarray:
    """Upright person filling a tall box. Elevated view bends one knee in image."""
    k = _blank()
    k[0] = [240, 70, 0.9]
    k[5], k[6] = [160, 140, 0.9], [320, 130, 0.9]
    k[11], k[12] = [200, 310, 0.9], [280, 300, 0.9]
    k[13], k[14] = [120, 450, 0.9], [300, 460, 0.9]
    k[15], k[16] = [210, 660, 0.9], [270, 670, 0.9]
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
    foreshortened = classify_frame(
        foreshortened_standing_kpts(),
        box=(180, 80, 300, 420),
        frame_hw=(720, 1280),
    )
    assert foreshortened.label == "standing", foreshortened
    sit = classify_frame(sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280))
    assert sit.label == "sitting", sit
    reclined_sit = classify_frame(
        reclining_sitting_kpts(), box=(160, 100, 400, 380), frame_hw=(720, 1280)
    )
    assert reclined_sit.label == "sitting", reclined_sit
    wide_box_stand = classify_frame(
        standing_kpts(), box=(50, 300, 500, 420), frame_hw=(720, 1280)
    )
    assert wide_box_stand.label == "standing", wide_box_stand
    ly = classify_frame(lying_kpts(), box=(100, 180, 500, 280), frame_hw=(720, 1280))
    assert ly.label == "lying", ly
    up = classify_frame(
        upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
    )
    assert up.label == "unknown", up
    oh_sit = classify_frame(
        overhead_sitting_kpts(), box=(250, 160, 400, 370), frame_hw=(480, 640)
    )
    assert oh_sit.label == "sitting", oh_sit
    oh_lie = classify_frame(
        overhead_lying_kpts(), box=(240, 190, 400, 360), frame_hw=(480, 640)
    )
    assert oh_lie.label == "lying", oh_lie
    bed = classify_frame(
        bed_supine_kpts(), box=(220, 150, 430, 420), frame_hw=(1080, 1920)
    )
    assert bed.label == "lying", bed
    along = classify_frame(
        along_camera_lying_kpts(), box=(220, 120, 460, 400), frame_hw=(1080, 1920)
    )
    assert along.label == "lying", along
    tall_stand = classify_frame(
        tall_silhouette_standing_kpts(),
        box=(100, 40, 380, 720),
        frame_hw=(1080, 1920),
    )
    assert tall_stand.label == "standing", tall_stand
    print(
        "classify_frame OK:",
        s.label,
        foreshortened.label,
        sit.label,
        reclined_sit.label,
        wide_box_stand.label,
        ly.label,
        up.label,
        oh_sit.label,
        oh_lie.label,
        bed.label,
    )


def test_posture_transition() -> None:
    """The displayed activity should follow a real sit-to-stand transition promptly."""
    tracker = ActivityTracker()
    for _ in range(20):
        state = tracker.update(
            sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280)
        )
    assert state.label == "sitting", state

    labels = [
        tracker.update(
            foreshortened_standing_kpts(),
            box=(180, 80, 300, 420),
            frame_hw=(720, 1280),
        ).label
        for _ in range(8)
    ]
    assert "standing" in labels[:6] and labels[-1] == "standing", labels
    print("sit-to-stand transition OK:", labels)


def test_unknown_when_pose_is_insufficient() -> None:
    no_pose = classify_frame(_blank())
    assert no_pose.label == "unknown" and no_pose.detail == "missing_torso", no_pose

    partial = classify_frame(
        upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
    )
    assert partial.label == "unknown", partial

    weak_legs = standing_kpts()
    weak_legs[13:17, 2] = 0.0
    weak = classify_frame(weak_legs, box=(180, 80, 300, 500), frame_hw=(720, 1280))
    assert weak.label == "unknown", weak

    tracker = ActivityTracker()
    for _ in range(8):
        tracker.update(
            standing_kpts(), box=(180, 80, 300, 500), frame_hw=(720, 1280)
        )
    state = None
    for _ in range(12):
        state = tracker.update(
            upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
        )
    assert state.label == "unknown", state
    print("unknown abstention OK:", no_pose.detail, partial.detail, weak.detail)


def test_activity_hysteresis_on_pose_dropout() -> None:
    tracker = ActivityTracker()
    for _ in range(12):
        state = tracker.update(
            sitting_kpts(), box=(160, 100, 320, 380), frame_hw=(720, 1280)
        )
    assert state.label == "sitting", state

    brief_dropout = tracker.update(
        upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
    )
    assert brief_dropout.label == "sitting", brief_dropout

    for _ in range(12):
        state = tracker.update(
            upper_only_kpts(), box=(80, 5, 560, 700), frame_hw=(720, 1280)
        )
    assert state.label == "unknown", state
    print("activity dropout hysteresis OK")


def test_walking() -> None:
    """Sideways walk + toward-camera walk must both register as walking."""
    t = ActivityTracker(window=20, walk_speed=0.022, walk_confirm=2)
    st = None
    for i in range(14):
        dx = i * 28.0  # ~person-height fraction per few frames
        box = (180 + dx, 80, 300 + dx, 500)
        st = t.update(
            standing_kpts(dx=dx),
            box=box,
            frame_hw=(720, 1280),
            timestamp=i / 8,
        )
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
        st2 = t2.update(k, box=box, frame_hw=(720, 1280), timestamp=i / 8)
    assert st2 is not None and st2.label == "walking", st2
    print("walking toward-camera OK:", st2.detail)

    # Equal physical movement over the same duration at different sample rates.
    for fps in (5, 10):
        tracker = ActivityTracker(window=20)
        state = None
        for i in range(fps * 3):
            dx = i * (100.0 / fps)
            state = tracker.update(
                standing_kpts(dx=dx),
                box=(180 + dx, 80, 300 + dx, 500),
                frame_hw=(720, 1280),
                timestamp=i / fps,
            )
        assert state is not None and state.label == "walking", (fps, state)
    print("walking frame-rate invariance OK")

    # A person lying still, and a standing person who only jitters, are not walking.
    still_bed = ActivityTracker()
    bed_state = None
    for i in range(16):
        jitter = 6.0 if i % 2 == 0 else -6.0
        bed_state = still_bed.update(
            bed_supine_kpts(),
            box=(220 + jitter, 150, 430 + jitter, 420),
            frame_hw=(1080, 1920),
            timestamp=i / 24,
        )
    assert bed_state is not None and bed_state.label == "lying", bed_state

    still_stand = ActivityTracker()
    stand_state = None
    for i in range(16):
        jitter = 8.0 if i % 2 == 0 else -8.0
        stand_state = still_stand.update(
            standing_kpts(dx=jitter),
            box=(180 + jitter, 80, 300 + jitter, 500),
            frame_hw=(720, 1280),
            timestamp=i / 24,
        )
    assert stand_state is not None and stand_state.label == "standing", stand_state
    print("still bed/stand not walking OK", bed_state.label, stand_state.label)

    # A stop is standing again. An earlier walk does not keep the label.
    paused = ActivityTracker()
    paused_state = None
    for i in range(12):
        dx = i * 30.0
        paused_state = paused.update(
            standing_kpts(dx=dx),
            box=(180 + dx, 80, 300 + dx, 500),
            frame_hw=(720, 1280),
            timestamp=i / 10,
        )
    assert paused_state is not None and paused_state.label == "walking", paused_state
    for i in range(8):
        paused_state = paused.update(
            standing_kpts(dx=11 * 30.0),
            box=(180 + 11 * 30.0, 80, 300 + 11 * 30.0, 500),
            frame_hw=(720, 1280),
            timestamp=1.2 + i / 10,
        )
    assert paused_state is not None and paused_state.label == "standing", paused_state
    print("walk then stop OK", paused_state.label)


def test_fall() -> None:
    t = ActivityTracker(window=18)
    for i in range(16):
        t.update(
            standing_kpts(),
            box=(180, 80, 300, 500),
            frame_hw=(720, 1280),
            timestamp=i / 8,
        )
    st = None
    for i in range(10):
        k = lying_kpts()
        k[:, 1] += 50 + i * 8
        st = t.update(
            k,
            box=(50, 300, 500, 420),
            frame_hw=(720, 1280),
            timestamp=(16 + i) / 8,
        )
    assert st is not None and st.label == "fall_suspected", st
    print("fall_suspected OK:", st.detail)

    for i in range(16):
        st = t.update(
            lying_kpts(),
            box=(50, 300, 500, 420),
            frame_hw=(720, 1280),
            timestamp=(26 + i) / 8,
        )
    assert st.label == "fall_suspected", st

    for i in range(10):
        st = t.update(
            standing_kpts(),
            box=(180, 80, 300, 500),
            frame_hw=(720, 1280),
            timestamp=(42 + i) / 8,
        )
    assert st.label != "fall_suspected", st
    print("fall alert latch/recovery OK")


def test_fall_rejects_posture_only_and_gaps() -> None:
    hw = (720, 1280)
    tracker = ActivityTracker()
    for i in range(20):
        state = tracker.update(
            standing_kpts(), box=(180, 80, 300, 500), frame_hw=hw, timestamp=i / 10
        )

    # A person may lie down without a fall; shape changes but the body does not
    # move down relative to the previous person-sized box.
    flat_box = (50, 230, 500, 350)
    for i in range(10):
        state = tracker.update(
            lying_kpts(), box=flat_box, frame_hw=hw, timestamp=2.0 + i / 10
        )
    assert state.label != "fall_suspected", state

    # A long unobserved gap must not be mistaken for an observed fall transition.
    tracker = ActivityTracker()
    for i in range(16):
        tracker.update(
            standing_kpts(), box=(180, 80, 300, 500), frame_hw=hw, timestamp=i / 8
        )
    state = None
    for i in range(10):
        state = tracker.update(
            lying_kpts(),
            box=(50, 300, 500, 420),
            frame_hw=hw,
            timestamp=4.0 + i / 8,
        )
    assert state is not None and state.label != "fall_suspected", state

    # Sitting/reclining with a large box shift is not an observed lying fall.
    tracker = ActivityTracker()
    for i in range(16):
        tracker.update(
            standing_kpts(),
            box=(180, 80, 300, 500),
            frame_hw=hw,
            timestamp=i / 8,
        )
    state = None
    for i in range(10):
        state = tracker.update(
            reclining_sitting_kpts(),
            box=(160, 220, 400, 500),
            frame_hw=hw,
            timestamp=(16 + i) / 8,
        )
    assert state is not None and state.label != "fall_suspected", state
    print("fall false-positive guards OK")


def test_stable_person_ids() -> None:
    ids = StablePersonIds(max_age=12)
    first = np.array([[10, 10, 110, 210], [300, 10, 400, 210]], dtype=float)
    initial = ids.update(first, [7, 8])
    assert initial == [1, 2], initial

    # Detection order changes and the first track receives a new raw tracker ID.
    reordered = np.array([[302, 10, 402, 210], [12, 10, 112, 210]], dtype=float)
    continued = ids.update(reordered, [8, 99])
    assert continued == [2, 1], continued
    assert 7 not in ids.raw_to_stable

    # With raw IDs missing, overlap association still follows the person boxes.
    no_tracker_ids = ids.update(reordered[::-1], [None, None])
    assert no_tracker_ids == [1, 2], no_tracker_ids
    reused_raw = ids.update(first, [7, 8])
    assert reused_raw == [1, 2], reused_raw
    print("stable person IDs OK:", initial, continued, no_tracker_ids)


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
        PersonView(1, a1, (0, 0, 100, 80), 8000),
        PersonView(2, a2, (0, 0, 50, 120), 6000),
        PersonView(3, a3, (0, 0, 60, 140), 7000),
    ]
    sc = summarize(people)
    assert sc.people_count == 3
    assert sc.fall_alerts == 0
    assert any(p.activity.label == "walking" for p in sc.people)
    print(
        "scene OK:",
        f"people={sc.people_count} falls={sc.fall_alerts}",
        [(p.track_id, p.activity.label) for p in sc.people],
    )


if __name__ == "__main__":
    test_all_labels_covered()
    test_classify()
    test_posture_transition()
    test_unknown_when_pose_is_insufficient()
    test_activity_hysteresis_on_pose_dropout()
    test_walking()
    test_fall()
    test_fall_rejects_posture_only_and_gaps()
    test_stable_person_ids()
    test_scene()
    print("ALL_UNIT_OK")
