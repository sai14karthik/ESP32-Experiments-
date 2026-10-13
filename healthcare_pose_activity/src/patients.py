"""Patient-oriented labeling helpers (prototype — not identity recognition)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from activity import ActivityState


# Activities typical of a monitored patient in room/bed context
PATIENT_LIKE = frozenset({"lying", "sitting", "fall_suspected"})
MOBILE_LIKE = frozenset({"walking", "standing"})


@dataclass
class PersonView:
    track_id: int
    activity: ActivityState
    role: str  # patient | visitor_or_staff | unknown
    box: tuple[float, float, float, float]  # xyxy
    area: float = 0.0


@dataclass
class SceneSummary:
    people_count: int = 0
    patients: int = 0
    others: int = 0
    fall_alerts: int = 0
    people: list[PersonView] = field(default_factory=list)


def infer_role(activity: ActivityState, *, primary: bool) -> str:
    """
    Heuristic role for room monitoring (not face/ID).
    - Lying / fall / sitting (often bed/chair) → patient
    - Walking → visitor_or_staff
    - Standing: patient if largest person in frame else visitor_or_staff
    """
    lab = activity.label
    if lab in ("lying", "fall_suspected"):
        return "patient"
    if lab == "sitting":
        return "patient"
    if lab == "walking":
        return "visitor_or_staff"
    if lab == "standing":
        return "patient" if primary else "visitor_or_staff"
    return "unknown"


def summarize(people: list[PersonView]) -> SceneSummary:
    patients = sum(1 for p in people if p.role == "patient")
    others = sum(1 for p in people if p.role == "visitor_or_staff")
    falls = sum(1 for p in people if p.activity.label == "fall_suspected")
    return SceneSummary(
        people_count=len(people),
        patients=patients,
        others=others,
        fall_alerts=falls,
        people=people,
    )
