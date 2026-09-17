"""目标轨道 → 深度调研的注入链路。

验证：``dispatch_research(track=…)`` 能把对应场景的输出模板送进调研子 Agent 的
system prompt，且未传 track 时按「用户启用轨道 → 默认轨道」回退。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_assistant.config import settings
from agent_assistant.goals.registry import get_spec
from agent_assistant.goals.store import GoalStore, goal_store
from agent_assistant.tools import research


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture
def store(tmp_path, monkeypatch) -> GoalStore:
    s = GoalStore(tmp_path / "goals.db")
    monkeypatch.setattr(goal_store, "_store", s)
    return s


def _final(text: str = "# 报告\n正文") -> SimpleNamespace:
    msg = SimpleNamespace(tool_calls=None, content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _run(goal: str = "对比四校计算机专硕", **kw) -> str:
    """跑一次调研，返回捕获到的 system prompt。"""
    captured: dict[str, str] = {}

    def chat(messages, tools, **_kwargs):
        captured["system"] = messages[0]["content"]
        return _final()

    with patch.object(research.llm_client, "chat", side_effect=chat):
        research.run_research_subagent(goal=goal, **kw)
    return captured.get("system", "")


def test_postgrad_template_injected(tmp_data_dir):
    system = _run(track="postgrad")
    assert "研究生择校" in system
    assert "初试科目" in system
    assert "严禁推测、严禁补全" in system
    assert "目标轨道：考研" in system
    for field in get_spec("postgrad").output_fields:
        assert field in system


def test_job_template_injected(tmp_data_dir):
    system = _run(track="job")
    assert "求职选岗" in system
    assert "薪资区间" in system


def test_no_template_without_track(tmp_data_dir, store):
    """没有目标轨道 → 不注入任何场景模板，调研保持通用。"""
    system = _run()
    assert "目标轨道" not in system
    assert "研究生择校" not in system


def test_unknown_track_ignored_without_crashing(tmp_data_dir, store):
    """未知 track 值不该炸，也不该偷偷套用别人的模板。

    必须挂 ``store`` fixture：否则会读到用户真实 goals.db 里已启用的轨道。
    """
    system = _run(track="not_a_track")
    assert "目标轨道" not in system


def test_falls_back_to_active_track(tmp_data_dir, store):
    store.create_track(kind="job", title="秋招", cycle_year=2027)
    system = _run()
    assert "求职选岗" in system


def test_explicit_track_beats_active_track(tmp_data_dir, store):
    store.create_track(kind="job", title="秋招", cycle_year=2027)
    system = _run(track="postgrad")
    assert "研究生择校" in system


def test_dispatch_tool_exposes_track_param():
    tool = research.DispatchResearchTool()
    names = {p.name for p in tool.parameters}
    assert "track" in names
    track_param = next(p for p in tool.parameters if p.name == "track")
    assert track_param.required is False
    assert "考研" in track_param.description


def test_researcher_runner_forwards_structured_events(monkeypatch):
    """on_event 必须一路透传到卡片事件总线（thinking / tool_call / tool_result）。"""
    import threading

    from agent_assistant.subagents.manager import TaskControls
    from agent_assistant.subagents.models import SubagentRole, SubagentSpec
    from agent_assistant.subagents.runners.researcher import researcher_runner
    from agent_assistant.tools.base import ToolResult

    seen: list[tuple[str, dict]] = []

    def fake_run(goal, context="", resume_from=None, track=None, depth=None, *,
                 cancel_check=None, on_progress=None, on_event=None):
        on_event("thinking", {"text": "先查官网", "turn": 1})
        on_event("tool_call", {"tool": "web_search", "label": "搜索：东北大学"})
        on_event("tool_result", {"tool": "web_search", "ok": True, "count": 3})
        return ToolResult.success(data={"report": "报告", "summary": "摘要"})

    monkeypatch.setattr(research, "run_research_subagent", fake_run)

    spec = SubagentSpec(
        task_id="t1",
        conversation_id="c1",
        parent_turn_id="p1",
        role=SubagentRole.RESEARCHER,
        title="择校调研",
        goal="对比四校",
    )
    controls = TaskControls("t1", threading.Event(), threading.Event())
    researcher_runner(spec, controls, lambda t, p: seen.append((t, p)))

    kinds = [k for k, _ in seen]
    assert kinds == ["thinking", "tool_call", "tool_result"]
    assert seen[0][1]["text"] == "先查官网"
    assert seen[2][1]["count"] == 3


def test_researcher_runner_forwards_track_from_spec_context(monkeypatch):
    """researcher_runner 必须把 spec.context['track'] 透传给调研执行器。"""
    import threading

    from agent_assistant.subagents.manager import TaskControls
    from agent_assistant.subagents.models import SubagentRole, SubagentSpec
    from agent_assistant.subagents.runners.researcher import researcher_runner
    from agent_assistant.tools.base import ToolResult

    seen: dict = {}

    def fake_run(goal, context="", resume_from=None, track=None, **_kw):
        seen["track"] = track
        return ToolResult.success(data={"report": "报告", "summary": "摘要"})

    monkeypatch.setattr(research, "run_research_subagent", fake_run)

    spec = SubagentSpec(
        task_id="t1",
        conversation_id="c1",
        parent_turn_id="p1",
        role=SubagentRole.RESEARCHER,
        title="择校调研",
        goal="对比四校计算机专硕",
        context={"track": "civil_service"},
    )
    controls = TaskControls("t1", threading.Event(), threading.Event())
    researcher_runner(spec, controls, lambda *_a: None)
    assert seen["track"] == "civil_service"


def test_manager_routing_carries_track(monkeypatch):
    """_execute_via_manager 必须把 track 塞进 SubagentSpec.context。"""
    seen: dict = {}

    class _FakeManager:
        def dispatch(self, specs):
            seen["context"] = specs[0].context
            return ["task-1"]

        def await_tasks(self, *_a, **_k):
            return []

        def control(self, *_a, **_k):
            return None

    class _Ctx:
        conversation_id = "c1"
        parent_turn_id = "t1"
        cancel_event = SimpleNamespace(is_set=lambda: False)

    monkeypatch.setattr(
        "agent_assistant.subagents.context.current_execution_context",
        lambda: _Ctx(),
    )
    monkeypatch.setattr(
        "agent_assistant.subagents.service.get_manager", lambda: _FakeManager()
    )

    research.DispatchResearchTool._execute_via_manager(
        "对比四校", "背景", None, "civil_service"
    )
    assert seen["context"]["track"] == "civil_service"
