"""Goal tracks — pluggable per-goal configuration for study/life goals.

Three concepts, keep them straight:

- **TrackSpec** (``registry.py``) — read-only config for one *kind* of goal
  (考研 / 考公 / 求职). Code, frozen, one per kind.
- **GoalTrack** (``models.py``) — one concrete user goal. Data, mutable,
  several at once, persisted in SQLite.
- **GoalStore** (``store.py``) — persistence + milestone materialization.

Adding a fourth goal = one new spec file + one entry in ``registry.TRACKS``.
No other module should ever branch on ``TrackKind``.
"""

from agent_assistant.goals.models import (
    GoalTrack,
    Milestone,
    TrackKind,
    TrackMilestone,
    TrackSpec,
    TrackStatus,
    infer_cycle_year,
)
from agent_assistant.goals.registry import (
    DEFAULT_KIND,
    TRACKS,
    all_specs,
    available_specs,
    get_spec,
    label_of,
    resolve_spec,
)
from agent_assistant.goals.store import GoalStore, goal_store

__all__ = [
    "DEFAULT_KIND",
    "TRACKS",
    "GoalStore",
    "GoalTrack",
    "Milestone",
    "TrackKind",
    "TrackMilestone",
    "TrackSpec",
    "TrackStatus",
    "all_specs",
    "available_specs",
    "get_spec",
    "goal_store",
    "infer_cycle_year",
    "label_of",
    "resolve_spec",
]
