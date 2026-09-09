"""Researcher runner adapter + injected callbacks (Tasks 8-9)."""

from __future__ import annotations

import threading

import agent_assistant.tools.research as research_module
from agent_assistant.subagents.manager import TaskControls
from agent_assistant.subagents.models import SubagentRole, SubagentSpec, SubagentStatus
from agent_assistant.subagents.runners.researcher import researcher_runner
from agent_assistant.tools.base import ToolResult


def make_spec(**overrides) -> SubagentSpec:
    values = dict(
        task_id="task-r1",
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=SubagentRole.RESEARCHER,
        title="调研考研政策",
        goal="调研 2026 年考研最新政策",
    )
    values.update(overrides)
    return SubagentSpec(**values)


def make_controls() -> TaskControls:
    return TaskControls("task-r1", threading.Event(), threading.Event())


SUCCESS_DATA = {
    "summary": "2026 年考研政策有三大变化……",
    "findings": [
        {"claim": "初试时间提前一周", "sources": ["https://example.com/a"], "l1_refs": [1]},
        {"claim": "扩招管理类硕士", "sources": ["https://example.com/b"], "l1_refs": [2]},
    ],
    "report": "# 报告\n\n详细内容……",
    "turns_used": 12,
    "budget": 60,
    "findings_count": 2,
    "run_id": "run-abc",
    "l1_trace": "C:/data/research_runs/run-abc/trace.jsonl",
    "l2_findings": "C:/data/research_runs/run-abc/findings.json",
    "l0_raw": "C:/data/research_runs/run-abc/raw.jsonl",
}


def test_completed_result_maps_report_without_copying_into_summary(monkeypatch) -> None:
    captured = {}

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        captured["goal"] = goal
        captured["context"] = context
        captured["cancel_check"] = cancel_check
        captured["on_progress"] = on_progress
        return ToolResult.success(data=dict(SUCCESS_DATA))

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)

    events: list[tuple[str, dict]] = []
    result = researcher_runner(
        make_spec(context={"background": "用户是应届生"}),
        make_controls(),
        lambda event_type, payload: events.append((event_type, payload)),
    )

    assert result.status is SubagentStatus.COMPLETED
    assert result.output.startswith("# 报告")
    assert "# 报告" not in result.summary  # headline only
    assert len(result.summary) <= 401
    assert result.artifacts["run_id"] == "run-abc"
    assert result.artifacts["l1_trace"].endswith("trace.jsonl")
    assert result.l0_raw and result.l1_trace
    assert result.evidence[0]["ref"] == "https://example.com/a"
    assert result.stats["turns_used"] == 12
    assert captured["goal"] == "调研 2026 年考研最新政策"
    assert "应届生" in captured["context"]


def test_failure_maps_partial_result_and_resume_artifacts(monkeypatch) -> None:
    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        return ToolResult.failure(
            "sub-agent LLM timed out twice at turn 9/60",
            error_category="subagent_timeout",
        )

    # Failure ToolResult without data → adapter still produces a clean result.
    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    result = researcher_runner(make_spec(), make_controls(), lambda t, p: None)
    assert result.status is SubagentStatus.FAILED
    assert result.error_category == "subagent_timeout"
    assert result.partial_result is not None


def test_failure_with_salvage_data_preserves_findings(monkeypatch) -> None:
    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        return ToolResult(
            ok=False,
            error="timeout",
            error_category="subagent_timeout",
            data={
                "summary": "- 已找到两条政策线索",
                "findings": SUCCESS_DATA["findings"],
                "resume_from": "run-abc",
                "turns_used": 9,
            },
        )

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    result = researcher_runner(make_spec(), make_controls(), lambda t, p: None)
    assert result.status is SubagentStatus.FAILED
    assert result.partial_result["resume_from"] == "run-abc"
    assert len(result.partial_result["findings"]) == 2
    assert "run-abc" in (result.next_action or "")


def test_cancelled_run_maps_to_cancelled_result(monkeypatch) -> None:
    controls = make_controls()

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        controls.cancel_event.set()
        return ToolResult.failure(
            "research cancelled by user", error_category="user_cancelled"
        )

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    result = researcher_runner(make_spec(), controls, lambda t, p: None)
    assert result.status is SubagentStatus.CANCELLED


def test_two_parallel_researchers_use_distinct_cancel_and_progress(monkeypatch) -> None:
    seen: dict[str, dict] = {}

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        on_progress("web_search", f"搜索 {goal}", 1, 60)
        seen[goal] = {"cancelled": cancel_check()}
        return ToolResult.success(data=dict(SUCCESS_DATA))

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)

    controls_a = TaskControls("task-a", threading.Event(), threading.Event())
    controls_b = TaskControls("task-b", threading.Event(), threading.Event())
    controls_a.cancel_event.set()  # cancel A only

    events_a: list[dict] = []
    events_b: list[dict] = []
    researcher_runner(
        make_spec(task_id="task-a", goal="目标A"),
        controls_a,
        lambda t, p: events_a.append(p),
    )
    researcher_runner(
        make_spec(task_id="task-b", goal="目标B"),
        controls_b,
        lambda t, p: events_b.append(p),
    )

    assert seen["目标A"]["cancelled"] is True
    assert seen["目标B"]["cancelled"] is False
    assert events_a[0]["label"] == "搜索 目标A"
    assert events_b[0]["label"] == "搜索 目标B"


def test_continue_instruction_and_resume_from_flow_into_run(monkeypatch) -> None:
    captured = {}

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None):
        captured["context"] = context
        captured["resume_from"] = resume_from
        return ToolResult.success(data=dict(SUCCESS_DATA))

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    researcher_runner(
        make_spec(
            context={
                "background": "背景",
                "continue_instruction": "补充对比 2025 年数据",
                "resume_from": "run-old",
            }
        ),
        make_controls(),
        lambda t, p: None,
    )
    assert "补充对比 2025 年数据" in captured["context"]
    assert captured["resume_from"] == "run-old"


def test_call_llm_guarded_honors_injected_cancel_check(monkeypatch) -> None:
    """The guarded LLM call polls the injected token, not the global one."""

    started = threading.Event()

    def slow_chat(**kwargs):
        started.set()
        threading.Event().wait(3)
        return None

    monkeypatch.setattr(research_module.llm_client, "chat", slow_chat)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    response, error = research_module._call_llm_guarded(
        [], [], timeout_s=10, poll_interval=0.05, cancel_check=cancel.is_set
    )
    assert response is None
    assert error == "cancelled"
    assert started.wait(timeout=1)


# ─── dispatch_research compatibility wrapper (Task 9) ─────────────────────────


def test_dispatch_research_without_context_uses_direct_path(monkeypatch) -> None:
    called = {}

    def fake_run(goal, context="", local_sources=None, use_knowledge=False, resume_from=None,
                 **_kwargs):
        called["goal"] = goal
        return ToolResult.success(data=dict(SUCCESS_DATA))

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    monkeypatch.setattr(research_module, "take_pending_local_sources", lambda: [])
    monkeypatch.setattr(
        "agent_assistant.context_attachment.take_use_knowledge", lambda: False
    )
    result = research_module.DispatchResearchTool().execute(goal="调研目标")
    assert result.ok is True
    assert called["goal"] == "调研目标"
    assert result.data["run_id"] == "run-abc"


def test_dispatch_research_with_context_routes_through_manager(
    tmp_path, monkeypatch
) -> None:
    from agent_assistant.subagents.context import (
        AgentExecutionContext,
        bind_execution_context,
    )
    from agent_assistant.subagents.manager import SubagentManager
    from agent_assistant.subagents.resources import ResourceCoordinator
    from agent_assistant.subagents.store import SubagentStore

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None,
                 **_kwargs):
        on_progress("web_search", "搜索政策", 1, 60)
        return ToolResult.success(data=dict(SUCCESS_DATA))

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    monkeypatch.setattr(research_module, "take_pending_local_sources", lambda: [])
    monkeypatch.setattr(
        "agent_assistant.context_attachment.take_use_knowledge", lambda: False
    )

    store = SubagentStore(tmp_path / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: researcher_runner,
        archive_root=tmp_path / "runs",
        resources=ResourceCoordinator(),
    )
    monkeypatch.setattr(
        "agent_assistant.subagents.service.get_manager", lambda: manager
    )
    try:
        context = AgentExecutionContext(
            conversation_id="conv-9",
            parent_turn_id="turn-9",
            owner_id="main:conv-9:turn-9:x",
            cancel_event=threading.Event(),
        )
        with bind_execution_context(context):
            result = research_module.DispatchResearchTool().execute(goal="调研目标")
        assert result.ok is True
        # Legacy fields reconstructed for the model/UI.
        assert result.data["report"].startswith("# 报告")
        assert result.data["summary"]
        assert result.data["run_id"] == "run-abc"
        assert result.data["findings"][0]["sources"] == ["https://example.com/a"]
        assert result.data["task_id"]
        # The task went through the manager and is persisted for this conv.
        tasks = manager.list_tasks(conversation_id="conv-9")
        assert len(tasks) == 1
        assert tasks[0].role is SubagentRole.RESEARCHER
        assert tasks[0].parent_turn_id == "turn-9"
    finally:
        manager.shutdown()
        store.close()


def test_manager_routed_failure_keeps_salvage_fields(tmp_path, monkeypatch) -> None:
    from agent_assistant.subagents.context import (
        AgentExecutionContext,
        bind_execution_context,
    )
    from agent_assistant.subagents.manager import SubagentManager
    from agent_assistant.subagents.resources import ResourceCoordinator
    from agent_assistant.subagents.store import SubagentStore

    def fake_run(goal, context="", resume_from=None, *, cancel_check=None, on_progress=None,
                 **_kwargs):
        return ToolResult(
            ok=False,
            error="timeout",
            error_category="subagent_timeout",
            data={
                "summary": "- 部分线索",
                "findings": SUCCESS_DATA["findings"],
                "resume_from": "run-abc",
            },
        )

    monkeypatch.setattr(research_module, "run_research_subagent", fake_run)
    monkeypatch.setattr(research_module, "take_pending_local_sources", lambda: [])
    monkeypatch.setattr(
        "agent_assistant.context_attachment.take_use_knowledge", lambda: False
    )

    store = SubagentStore(tmp_path / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: researcher_runner,
        archive_root=tmp_path / "runs",
        resources=ResourceCoordinator(),
    )
    monkeypatch.setattr(
        "agent_assistant.subagents.service.get_manager", lambda: manager
    )
    try:
        context = AgentExecutionContext(
            conversation_id="conv-9",
            parent_turn_id="turn-9",
            owner_id="main:conv-9:turn-9:x",
            cancel_event=threading.Event(),
        )
        with bind_execution_context(context):
            result = research_module.DispatchResearchTool().execute(goal="调研目标")
        assert result.ok is False
        assert result.error_category == "subagent_timeout"
        assert result.data["resume_from"] == "run-abc"
        assert result.data["next_action"]["args"]["resume_from"] == "run-abc"
        assert result.data["summary"] == "- 部分线索"
    finally:
        manager.shutdown()
        store.close()


def test_service_registers_researcher_runner(tmp_path, monkeypatch) -> None:
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import service

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    service.reset_for_tests()
    try:
        manager = service.get_manager()
        assert manager is not None
        assert SubagentRole.RESEARCHER in service.registered_roles()
        runner = service.runner_for(SubagentRole.RESEARCHER)
        assert runner is researcher_runner
    finally:
        service.reset_for_tests()
