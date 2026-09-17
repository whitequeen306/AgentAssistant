"""Goal-track domain models — types only, no I/O.

A *track* is one concrete goal the user is pursuing (2027 考研 · 计算机专硕,
2027 国考 · 税务系统, 2027 秋招 · 后端开发 …). Tracks are user data: mutable,
possibly several at once, persisted in SQLite (see ``store.py``).

A *spec* is the read-only configuration describing how to handle one kind of
track (考研 / 考公 / 求职 …). Specs are code: frozen, one per kind, hardcoded.

The split matters: everything that differs between 考研/考公/求职 lives in the
spec as **data**, so no caller ever writes ``if kind == "postgrad"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any


class TrackKind(StrEnum):
    """Supported goal kinds. The string value is the stable registry key."""

    POSTGRAD = "postgrad"            # 考研
    CIVIL_SERVICE = "civil_service"  # 考公
    JOB = "job"                      # 求职


class TrackStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Milestone:
    """One key date in a goal's timeline.

    Specs only carry *defaults* — real dates float every year and must stay
    user-editable. ``year_offset`` is relative to the track's ``cycle_year``
    (考研 2027: 初试 sits in 2026 → offset -1; 调剂 sits in 2027 → offset 0).
    """

    key: str
    label: str
    month: int  # 1-12
    day: int = 1  # 1-31
    year_offset: int = 0
    note: str = ""
    remind_before_days: int = 7

    def default_date(self, cycle_year: int) -> date:
        """Resolve the default date for a given cycle year.

        Clamps the day to the month length so 02-30 → 02-28/29 instead of
        raising (leap-year safe, never crashes on a spec typo).
        """
        year = cycle_year + self.year_offset
        month = max(1, min(12, self.month))
        last_day = _days_in_month(year, month)
        return date(year, month, max(1, min(self.day, last_day)))


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def infer_cycle_year(today: date | None = None) -> int:
    """Default cycle year for a newly created track.

    考研 / 国考 / 秋招的周期都跨自然年（2027 考研的初试在 2026 年 12 月），
    约定：**下半年起算下一年**。7 月之前创建算当年周期，7 月起算下一年周期。
    """
    d = today or date.today()
    return d.year + 1 if d.month >= 7 else d.year


@dataclass(frozen=True, slots=True)
class TrackSpec:
    """Immutable configuration template for one goal kind.

    Consumed by five places, none of which branch on ``kind``:
      - ``research_template`` → ``build_research_system_prompt(extra=…)``
      - ``output_fields``     → 对比表渲染 / 报告落库
      - ``milestones``        → daemon 到期提醒
      - ``prompt_hint``       → ``build_study_profile_section()``
      - ``chip_hints``        → ``ui/chips.py`` 开场建议
    """

    kind: TrackKind
    label: str
    research_template: str
    output_fields: tuple[str, ...]
    milestones: tuple[Milestone, ...]
    prompt_hint: str
    chip_hints: tuple[str, ...] = ()
    draft: bool = False  # True = 占位骨架，UI 应标灰且不建议用户启用

    def milestone(self, key: str) -> Milestone | None:
        for m in self.milestones:
            if m.key == key:
                return m
        return None

    def render_research_template(
        self,
        count: int = 0,
        cycle_year: int | None = None,
    ) -> str:
        """Fill ``{count}`` and the cycle-year placeholders.

        Plain ``str.replace`` — the template is authored by us and must never
        go through ``str.format`` (a stray brace in prose would explode).

        Supported placeholders: ``{count}``、``{cycle_year}``（本届，如 2027）、
        ``{cycle_prev_year}``（上一届，如 2026）、``{cycle_prev_years}``
        （近三个招生年度，如 "2026、2025、2024"）。

        ``cycle_year`` 缺省时回落到 ``infer_cycle_year()`` —— 模板里若留着裸
        ``{cycle_year}`` 比填错年份更糟。
        """
        year = cycle_year or infer_cycle_year()
        prev = year - 1
        text = self.research_template
        text = text.replace("{count}", str(count) if count > 0 else "各")
        text = text.replace("{cycle_year}", str(year))
        text = text.replace("{cycle_prev_year}", str(prev))
        text = text.replace("{cycle_prev_years}", f"{prev}、{prev - 1}、{prev - 2}")
        return text.strip()


@dataclass(slots=True)
class GoalTrack:
    """A persisted user goal (one row in ``goal_tracks``)."""

    track_id: str
    kind: str
    title: str
    status: str = TrackStatus.ACTIVE
    cycle_year: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_active(self) -> bool:
        return self.status == TrackStatus.ACTIVE


@dataclass(slots=True)
class TrackMilestone:
    """A materialized milestone row — spec default + user override."""

    track_id: str
    key: str
    label: str
    due_at: float  # epoch seconds
    done: bool = False
    note: str = ""


__all__ = [
    "GoalTrack",
    "Milestone",
    "TrackKind",
    "TrackMilestone",
    "TrackSpec",
    "TrackStatus",
    "infer_cycle_year",
]
