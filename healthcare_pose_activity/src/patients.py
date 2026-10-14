"""Per-person views and room-wide summaries for live activity monitoring."""
from __future__ import annotations

from dataclasses import dataclass, field

from activity import ActivityState


@dataclass
class PersonView:
    track_id: int
    activity: ActivityState
    box: tuple[float, float, float, float]  # xyxy
    area: float = 0.0


@dataclass
class SceneSummary:
    people_count: int = 0
    fall_alerts: int = 0
    people: list[PersonView] = field(default_factory=list)


def summarize(people: list[PersonView]) -> SceneSummary:
    falls = sum(1 for p in people if p.activity.label == "fall_suspected")
    return SceneSummary(
        people_count=len(people),
        fall_alerts=falls,
        people=people,
    )
