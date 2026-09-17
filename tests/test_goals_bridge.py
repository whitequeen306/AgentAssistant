"""学习库「规划」tab 的后端桥接层：目标轨道 CRUD + 节点读写。

``ApiBridge`` 的每个 goals 方法都必须**降级而非抛错**——前端用可选调用，
但桥接本身不能因为 DB 异常把 pywebview 调用搞崩。
"""

from __future__ import annotations

import datetime

import pytest

from agent_assistant.goals.store import GoalStore, goal_store
from agent_assistant.ui.bridge import ApiBridge


@pytest.fixture
def bridge(tmp_path, monkeypatch) -> ApiBridge:
    monkeypatch.setattr(goal_store, "_store", GoalStore(tmp_path / "goals.db"))
    return ApiBridge()


def test_list_goal_specs_exposes_draft_flag(bridge):
    res = bridge.list_goal_specs()
    assert res["ok"] is True
    by_kind = {s["kind"]: s for s in res["specs"]}
    assert by_kind["postgrad"]["draft"] is False
    assert by_kind["postgrad"]["milestone_count"] == 7
    assert by_kind["postgrad"]["output_fields"]
    assert by_kind["civil_service"]["draft"] is True


def test_create_then_list_round_trip(bridge):
    created = bridge.create_goal_track("postgrad", "2027 考研", 2027)
    assert created["ok"] is True
    assert created["milestone_count"] == 7

    tracks = bridge.list_goal_tracks()["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["title"] == "2027 考研"
    assert tracks[0]["kind_label"] == "考研"
    assert tracks[0]["cycle_year"] == 2027


def test_create_rejects_unknown_kind(bridge):
    res = bridge.create_goal_track("nope", "x")
    assert res["ok"] is False
    assert "未知" in res["error"]


def test_create_defaults_title_from_spec_label(bridge):
    res = bridge.create_goal_track("job", "")
    assert res["ok"] is True
    assert bridge.list_goal_tracks()["tracks"][0]["title"] == "求职目标"


def test_list_goal_tracks_filters_by_status(bridge):
    """不传 status = 全部（含归档，前端自行过滤）；传了才按状态筛。"""
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    assert len(bridge.list_goal_tracks("active")["tracks"]) == 1

    bridge.update_goal_track(tid, status="archived")
    assert len(bridge.list_goal_tracks()["tracks"]) == 1
    assert bridge.list_goal_tracks("active")["tracks"] == []
    assert len(bridge.list_goal_tracks("archived")["tracks"]) == 1


def test_milestones_are_created_and_readable(bridge):
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    ms = bridge.list_goal_milestones(tid)["milestones"]
    assert [m["key"] for m in ms][0] == "pre_signup"
    exam = next(m for m in ms if m["key"] == "exam")
    assert datetime.datetime.fromtimestamp(exam["due_at"]).date() == datetime.date(
        2026, 12, 19
    )
    assert exam["done"] is False
    assert exam["label"] == "初试"


def test_set_milestone_date_and_done(bridge):
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    res = bridge.set_goal_milestone(tid, "exam", due_date="2026-12-25")
    assert res["ok"] is True
    exam = next(
        m for m in bridge.list_goal_milestones(tid)["milestones"] if m["key"] == "exam"
    )
    assert datetime.datetime.fromtimestamp(exam["due_at"]).date() == datetime.date(
        2026, 12, 25
    )

    bridge.set_goal_milestone(tid, "exam", done=True)
    exam = next(
        m for m in bridge.list_goal_milestones(tid)["milestones"] if m["key"] == "exam"
    )
    assert exam["done"] is True


def test_pinned_date_survives_cycle_year_change(bridge):
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    bridge.set_goal_milestone(tid, "exam", due_date="2026-12-25")
    bridge.update_goal_track(tid, cycle_year=2029)

    ms = {m["key"]: m for m in bridge.list_goal_milestones(tid)["milestones"]}
    assert (
        datetime.datetime.fromtimestamp(ms["exam"]["due_at"]).date()
        == datetime.date(2026, 12, 25)
    )  # pinned
    assert (
        datetime.datetime.fromtimestamp(ms["signup"]["due_at"]).date()
        == datetime.date(2028, 10, 8)
    )  # moved


def test_set_milestone_rejects_bad_date(bridge):
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    res = bridge.set_goal_milestone(tid, "exam", due_date="2026/12/25")
    assert res["ok"] is False
    assert "YYYY-MM-DD" in res["error"]


def test_set_milestone_unknown_key(bridge):
    tid = bridge.create_goal_track("postgrad", "a", 2027)["track_id"]
    assert bridge.set_goal_milestone(tid, "nope", done=True)["ok"] is False


def test_update_and_delete_missing_track(bridge):
    assert bridge.update_goal_track("missing", title="x")["ok"] is False
    assert bridge.delete_goal_track("missing")["ok"] is False


def test_bridge_degrades_instead_of_raising(bridge, monkeypatch):
    """DB 炸了也要返回 {ok: False}，不能把异常抛给 pywebview。"""

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(GoalStore, "list_tracks", boom)
    res = bridge.list_goal_tracks()
    assert res["ok"] is False
    assert res["tracks"] == []
