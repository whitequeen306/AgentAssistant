"""Goal-track spec registry — the single lookup point for ``TrackSpec``.

Deliberately dependency-free (models + specs only). Callers never branch on
``kind``; they ask for a spec and read its fields:

    spec = get_spec("postgrad")
    build_research_system_prompt(extra=spec.render_research_template(count=4))
"""

from __future__ import annotations

import logging

from agent_assistant.goals.models import TrackKind, TrackSpec, infer_cycle_year
from agent_assistant.goals.specs import CIVIL_SERVICE, JOB, POSTGRAD

logger = logging.getLogger(__name__)

TRACKS: dict[TrackKind, TrackSpec] = {
    spec.kind: spec for spec in (POSTGRAD, CIVIL_SERVICE, JOB)
}

#: UI 新建轨道时的预选项（仅用于界面默认勾选，不参与调研回落）
DEFAULT_KIND = TrackKind.POSTGRAD


def all_specs() -> list[TrackSpec]:
    """Every registered spec, including drafts (for admin/debug views)."""
    return list(TRACKS.values())


def available_specs() -> list[TrackSpec]:
    """Specs the user can actually enable — drafts excluded."""
    return [s for s in TRACKS.values() if not s.draft]


def get_spec(kind: TrackKind | str | None) -> TrackSpec | None:
    """Look up a spec by ``TrackKind`` or its string value. Unknown → None."""
    if kind is None:
        return None
    if isinstance(kind, TrackKind):
        return TRACKS.get(kind)
    try:
        return TRACKS.get(TrackKind(str(kind).strip()))
    except ValueError:
        return None


def label_of(kind: TrackKind | str | None) -> str:
    spec = get_spec(kind)
    return spec.label if spec else ""


def resolve_spec(preferred: TrackKind | str | None = None) -> TrackSpec | None:
    """Resolve the spec to use for a research run.

    Order: explicit ``preferred`` → the user's active track → ``None``.

    ``None`` means "no goal template" — a user with no goal track must get a
    generic research report, **not** a 考研-shaped one. Silently defaulting to
    postgrad would force an 8-column 择校 table onto unrelated topics
    ("对比两个前端框架"), which is worse than injecting nothing.

    The store import is lazy on purpose: ``store`` imports ``registry``
    (to materialize milestones), so a top-level import would be circular.
    """
    spec = get_spec(preferred)
    if spec is not None:
        return spec

    try:
        from agent_assistant.goals.store import goal_store

        for track in goal_store.active_tracks():
            spec = get_spec(track.kind)
            if spec is not None:
                return spec
    except Exception as e:  # noqa: BLE001 — resolution is best-effort
        logger.debug("active-track resolution failed: %s", e)

    return None


def active_cycle_year() -> int:
    """The cycle the user is currently preparing for.

    Prefers the first active track's ``cycle_year``; falls back to
    ``infer_cycle_year()`` (from today's date) when there is no track or the
    track has no year set.

    Without this the research sub-agent has no idea which 届 it is serving and
    silently drifts to whichever year dominates the search results — it once
    produced a "2026 招生季" report in September 2026, when the catalogs being
    published at that moment were for 2027.
    """
    try:
        from agent_assistant.goals.store import goal_store

        for track in goal_store.active_tracks():
            if track.cycle_year:
                return int(track.cycle_year)
    except Exception as e:  # noqa: BLE001 — best-effort, same as resolve_spec
        logger.debug("active cycle year resolution failed: %s", e)

    return infer_cycle_year()


__all__ = [
    "DEFAULT_KIND",
    "TRACKS",
    "active_cycle_year",
    "all_specs",
    "available_specs",
    "get_spec",
    "label_of",
    "resolve_spec",
]
