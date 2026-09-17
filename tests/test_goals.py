"""Goal-track module: spec registry + store + milestone materialization.

Runs against a temp SQLite DB — the lazy singleton is swapped per test so the
real ``~/AgentAssistant/goals.db`` is never touched.
"""

from __future__ import annotations

import time
from datetime import date, datetime

import pytest

from agent_assistant.goals.models import (
    GoalTrack,
    Milestone,
    TrackKind,
    TrackStatus,
    infer_cycle_year,
)
from agent_assistant.goals.registry import (
    active_cycle_year,
    all_specs,
    available_specs,
    get_spec,
    label_of,
    resolve_spec,
)
from agent_assistant.goals.store import GoalStore, goal_store


@pytest.fixture
def store(tmp_path, monkeypatch) -> GoalStore:
    s = GoalStore(tmp_path / "goals.db")
    monkeypatch.setattr(goal_store, "_store", s)
    return s


# ─── Spec registry ──────────────────────────────────────────────────


def test_postgrad_spec_is_available_and_complete():
    spec = get_spec("postgrad")
    assert spec is not None
    assert spec.kind is TrackKind.POSTGRAD
    assert spec.label == "考研"
    assert spec.draft is False
    assert len(spec.output_fields) == 8
    assert len(spec.milestones) == 7
    assert spec.chip_hints
    assert spec.prompt_hint


def test_draft_specs_are_registered_but_hidden_from_users():
    assert get_spec("civil_service").draft is True
    assert get_spec("job").draft is True
    assert [s.kind for s in available_specs()] == [TrackKind.POSTGRAD]
    assert len(all_specs()) == 3


def test_get_spec_accepts_enum_and_rejects_unknown():
    assert get_spec(TrackKind.POSTGRAD) is get_spec("postgrad")
    assert get_spec("nope") is None
    assert get_spec(None) is None
    assert get_spec("") is None


def test_label_of_falls_back_to_empty():
    assert label_of("postgrad") == "考研"
    assert label_of("bogus") == ""


def test_research_template_renders_count_without_format_braces():
    spec = get_spec("postgrad")
    rendered = spec.render_research_template(count=4)
    assert "{count}" not in rendered
    assert "4" in rendered
    # 模板里其他花括号（Markdown 表格等）不应被当作占位符
    assert spec.render_research_template(0)


def test_every_spec_has_unique_milestone_keys():
    for spec in all_specs():
        keys = [m.key for m in spec.milestones]
        assert len(keys) == len(set(keys)), spec.kind


# ─── Milestone date math ────────────────────────────────────────────


def test_milestone_resolves_year_offset():
    spec = get_spec("postgrad")
    exam = spec.milestone("exam")
    assert exam is not None
    # 2027 考研的初试在 2026 年 12 月
    assert exam.default_date(2027) == date(2026, 12, 19)
    adjust = spec.milestone("adjust")
    assert adjust.default_date(2027) == date(2027, 4, 8)


def test_milestone_clamps_day_to_month_length():
    m = Milestone(key="x", label="x", month=2, day=30)
    assert m.default_date(2024) == date(2024, 2, 29)  # 闰年
    assert m.default_date(2027) == date(2027, 2, 28)
    assert Milestone(key="y", label="y", month=13, day=1).default_date(2027) == date(
        2027, 12, 1
    )


def test_infer_cycle_year_rolls_over_in_second_half():
    assert infer_cycle_year(date(2026, 9, 10)) == 2027
    assert infer_cycle_year(date(2026, 6, 1)) == 2026
    assert infer_cycle_year(date(2026, 7, 1)) == 2027


# ─── Store: tracks ──────────────────────────────────────────────────


def test_create_track_materializes_milestones(store):
    track = store.create_track(kind="postgrad", title="2027 考研 · 计算机专硕")
    assert isinstance(track, GoalTrack)
    assert track.status == TrackStatus.ACTIVE
    assert track.is_active

    ms = store.list_milestones(track.track_id)
    assert len(ms) == 7
    assert [m.key for m in ms][0] == "pre_signup"
    # 默认周期年份：下半年创建 → 下一年
    assert track.cycle_year == infer_cycle_year()


def test_milestone_defaults_use_cycle_year(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    by_key = {m.key: m for m in store.list_milestones(track.track_id)}
    exam = by_key["exam"]
    assert datetime.fromtimestamp(exam.due_at).date() == date(2026, 12, 19)


def test_multiple_tracks_coexist(store):
    a = store.create_track(kind="postgrad", title="考研", cycle_year=2027)
    b = store.create_track(kind="job", title="秋招", cycle_year=2027)
    assert len(store.active_tracks()) == 2

    store.archive_track(b.track_id)
    assert [t.track_id for t in store.active_tracks()] == [a.track_id]
    assert len(store.list_tracks()) == 2  # 归档不删除


def test_update_track_patches_config_and_moves_timeline(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    updated = store.update_track(
        track.track_id,
        title="2028 考研",
        cycle_year=2028,
        config={"target_schools": ["HIT", "HRBEU"]},
    )
    assert updated.title == "2028 考研"
    assert updated.config["target_schools"] == ["HIT", "HRBEU"]

    by_key = {m.key: m for m in store.list_milestones(track.track_id)}
    assert datetime.fromtimestamp(by_key["exam"].due_at).date() == date(2027, 12, 19)


def test_update_track_ignores_unknown_keys(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    assert store.update_track(track.track_id, bogus=1).title == track.title


def test_delete_track_cascades_milestones(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    assert store.delete_track(track.track_id) is True
    assert store.get_track(track.track_id) is None
    assert store.list_milestones(track.track_id) == []


# ─── Store: milestone overrides ─────────────────────────────────────


def test_pinned_milestone_survives_resync(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    custom = datetime(2026, 12, 25).timestamp()
    store.set_milestone(track.track_id, "exam", due_at=custom)

    store.sync_milestones(track.track_id)  # 未改年份，本不该动
    assert store.list_milestones(track.track_id)[3].due_at == custom

    store.update_track(track.track_id, cycle_year=2029)  # 改年份 → 重排
    by_key = {m.key: m for m in store.list_milestones(track.track_id)}
    assert by_key["exam"].due_at == custom  # 钉住的不动
    assert datetime.fromtimestamp(by_key["signup"].due_at).date() == date(2028, 10, 8)


def test_set_milestone_done_and_note(store):
    track = store.create_track(kind="postgrad", cycle_year=2027)
    m = store.set_milestone(track.track_id, "signup", done=True, note="已报")
    assert m.done is True
    assert m.note == "已报"

    by_key = {x.key: x for x in store.list_milestones(track.track_id)}
    assert by_key["signup"].done is True
    assert by_key["exam"].done is False


def test_done_milestone_is_not_due(store):
    track = store.create_track(kind="postgrad", cycle_year=2020)  # 全部过期
    for m in store.list_milestones(track.track_id):
        store.set_milestone(track.track_id, m.key, done=True)
    assert store.due_milestones(within_days=0) == []


def test_due_milestones_returns_upcoming_and_overdue(store):
    past = store.create_track(kind="postgrad", title="过期", cycle_year=2020)
    soon = store.create_track(kind="postgrad", title="临近", cycle_year=2027)
    target = datetime(2026, 12, 1).timestamp()
    store.set_milestone(soon.track_id, "exam", due_at=target)

    due = store.due_milestones(within_days=30, now=datetime(2026, 11, 20).timestamp())
    pairs = {(t.track_id, m.key) for t, m in due}
    assert (soon.track_id, "exam") in pairs
    assert any(t.track_id == past.track_id for t, _ in due)
    # 按到期时间升序
    assert [m.due_at for _, m in due] == sorted(m.due_at for _, m in due)


# ─── Resolution ─────────────────────────────────────────────────────


def test_resolve_spec_prefers_explicit_kind():
    assert resolve_spec("job").kind is TrackKind.JOB


def test_resolve_spec_returns_none_without_active_track(store):
    """没有目标轨道时**不注入任何模板**。

    回落成考研会让「对比两个前端框架」这类调研被套上八列择校表，比不注入更糟。
    """
    assert resolve_spec() is None


def test_resolve_spec_uses_first_active_track(store):
    store.create_track(kind="job", title="秋招", cycle_year=2027)
    assert resolve_spec().kind is TrackKind.JOB


def test_resolve_spec_is_stable_when_store_broken(monkeypatch):
    """DB 挂掉时回落到默认轨道，而不是把异常抛给调用方。

    注意：补丁打在 ``GoalStore`` 类上。打在 ``goal_store`` 懒加载单例上会让
    monkeypatch 在 teardown 时把真实 store 的绑定方法写回实例属性，污染后续测试。
    """

    def boom(self, *_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(GoalStore, "active_tracks", boom)
    assert resolve_spec() is None


class TestCycleYear:
    """年度口径：调研必须知道自己在服务哪一届。

    事故背景：2026 年 9 月跑四校择校调研，子 Agent 全程按「2026 招生季」搜
    （当时 2027 级目录正在陆续发布），产出一份滞后一代的报告。根因是模板里
    没有任何年份信息，模型只能跟着搜索结果里最常出现的年份走。
    """

    def test_infer_cycle_year_rolls_over_in_h2(self):
        from datetime import date

        assert infer_cycle_year(date(2026, 9, 11)) == 2027
        assert infer_cycle_year(date(2026, 6, 30)) == 2026
        assert infer_cycle_year(date(2026, 7, 1)) == 2027

    def test_active_cycle_year_uses_track_year(self, store):
        store.create_track(kind="postgrad", title="2028 考研", cycle_year=2028)
        assert active_cycle_year() == 2028

    def test_active_cycle_year_falls_back_to_inferred(self, store):
        assert active_cycle_year() == infer_cycle_year()

    def test_active_cycle_year_ignores_track_without_year(self, store):
        store.create_track(kind="postgrad", title="无年份")
        assert active_cycle_year() == infer_cycle_year()

    def test_active_cycle_year_survives_broken_store(self, monkeypatch):
        def boom(self, *_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(GoalStore, "active_tracks", boom)
        assert active_cycle_year() == infer_cycle_year()

    def test_template_fills_year_placeholders(self):
        text = get_spec("postgrad").render_research_template(cycle_year=2027)
        assert "2027 考研" in text
        # 2027 考研的报名与初试都在 2026 年（cycle_year - 1）
        assert "2026 年 12 月" in text
        assert "2026、2025、2024" in text
        assert "{cycle_year}" not in text
        assert "{cycle_prev_year}" not in text
        assert "{cycle_prev_years}" not in text

    def test_template_defaults_to_inferred_year(self):
        """不传年份时也要填，模板里留裸 ``{cycle_year}`` 比填错更糟。"""
        text = get_spec("postgrad").render_research_template()
        assert "{cycle_year}" not in text
        assert str(infer_cycle_year()) in text

    def test_count_placeholder_still_works_with_year(self):
        text = get_spec("postgrad").render_research_template(count=4, cycle_year=2027)
        assert "各" not in text.splitlines()[0] or "4" in text
