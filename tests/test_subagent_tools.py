"""dispatch/await/control orchestration tools (Task 7)."""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from agent_assistant.subagents.context import AgentExecutionContext, bind_execution_context
from agent_assistant.subagents.manager import SubagentManager
from agent_assistant.subagents.models import (
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)
from agent_assistant.subagents.resources import ResourceCoordinator
from agent_assistant.subagents.store import SubagentStore
from agent_assistant.tools.subagent_tools import (
    AwaitSubagentsTool,
    ControlSubagentTool,
    DispatchSubagentsTool,
)


@contextmanager
def bound_context(conversation_id: str = "conv-1", cancel_event=None):
    context = AgentExecutionContext(
        conversation_id=conversation_id,
        parent_turn_id="turn-1",
        owner_id=f"main:{conversation_id}:turn-1:abc",
        cancel_event=cancel_event or threading.Event(),
    )
    with bind_execution_context(context):
        yield context


def instant_runner(spec: SubagentSpec, controls, emit) -> SubagentResult:
    return SubagentResult.completed(
        task_id=spec.task_id,
        role=spec.role,
        attempt=spec.attempt,
        summary=f"完成 {spec.title}",
        output=f"output for {spec.goal}",
    )


@pytest.fixture()
def manager(tmp_path):
    store = SubagentStore(tmp_path / "subagents.db")
    mgr = SubagentManager(
        store,
        runner_for=lambda role: instant_runner,
        archive_root=tmp_path / "runs",
        resources=ResourceCoordinator(),
    )
    yield mgr
    mgr.shutdown()
    store.close()


def task_item(title: str = "查文件", role: str = "explorer", goal: str = "查找考研资料") -> dict:
    return {"title": title, "role": role, "goal": goal}


# ─── dispatch ────────────────────────────────────────────────────────────────


def test_dispatch_requires_bound_parent_context(manager) -> None:
    result = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
    assert result.ok is False
    assert result.error_category == "missing_execution_context"


def test_dispatch_accepts_at_most_four(manager) -> None:
    with bound_context():
        result = DispatchSubagentsTool(manager).execute(tasks=[task_item()] * 5)
    assert result.ok is False
    assert result.code == 400


def test_dispatch_uses_bound_identity_not_model_supplied(manager) -> None:
    with bound_context("conv-real"):
        result = DispatchSubagentsTool(manager).execute(
            tasks=[
                {
                    **task_item(),
                    # Model-supplied identity fields must be ignored.
                    "conversation_id": "conv-forged",
                    "parent_turn_id": "turn-forged",
                    "task_id": "forged-id",
                }
            ]
        )
    assert result.ok is True
    task_id = result.data["task_ids"][0]
    spec = manager.get_task(task_id)
    assert spec.conversation_id == "conv-real"
    assert spec.parent_turn_id == "turn-1"
    assert task_id != "forged-id"


def test_dispatch_validates_role_and_required_text(manager) -> None:
    with bound_context():
        bad_role = DispatchSubagentsTool(manager).execute(
            tasks=[task_item(role="coder")]
        )
        empty_goal = DispatchSubagentsTool(manager).execute(
            tasks=[task_item(goal="  ")]
        )
        not_object = DispatchSubagentsTool(manager).execute(tasks=["查文件"])
        empty = DispatchSubagentsTool(manager).execute(tasks=[])
    for result in (bad_role, empty_goal, not_object, empty):
        assert result.ok is False
        assert result.code == 400


def test_dispatch_passes_context_string_into_spec(manager) -> None:
    with bound_context():
        result = DispatchSubagentsTool(manager).execute(
            tasks=[{**task_item(), "context": "用户在 Documents 有一个考研文件夹"}]
        )
    assert result.ok is True
    spec = manager.get_task(result.data["task_ids"][0])
    assert spec.context["background"] == "用户在 Documents 有一个考研文件夹"


# ─── await ───────────────────────────────────────────────────────────────────


def test_await_returns_unified_results(manager) -> None:
    with bound_context():
        dispatched = DispatchSubagentsTool(manager).execute(
            tasks=[task_item("任务一"), task_item("任务二", goal="另一个目标")]
        )
        task_ids = dispatched.data["task_ids"]
        result = AwaitSubagentsTool(manager).execute(task_ids=task_ids, timeout_s=10)
    assert result.ok is True
    assert len(result.data["results"]) == 2
    assert result.data["pending"] == []
    statuses = {item["status"] for item in result.data["results"]}
    assert statuses == {"completed"}
    for item in result.data["results"]:
        assert item["task_id"] in task_ids
        assert item["summary"]


def test_await_requires_context_and_valid_ids(manager) -> None:
    no_context = AwaitSubagentsTool(manager).execute(task_ids=["t1"])
    assert no_context.error_category == "missing_execution_context"
    with bound_context():
        empty = AwaitSubagentsTool(manager).execute(task_ids=[])
        blank = AwaitSubagentsTool(manager).execute(task_ids=["  "])
        bad_timeout = AwaitSubagentsTool(manager).execute(task_ids=["t1"], timeout_s="soon")
    assert empty.code == 400 and blank.code == 400 and bad_timeout.code == 400


def test_await_rejects_foreign_conversation_tasks(manager) -> None:
    with bound_context("conv-a"):
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_ids = dispatched.data["task_ids"]
    with bound_context("conv-b"):
        result = AwaitSubagentsTool(manager).execute(task_ids=task_ids)
    assert result.ok is False
    assert result.error_category == "task_not_found"


def test_await_aborts_on_parent_cancel(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    block = threading.Event()

    def slow_runner(spec, controls, emit):
        block.wait(timeout=10)
        return instant_runner(spec, controls, emit)

    manager = SubagentManager(
        store,
        runner_for=lambda role: slow_runner,
        archive_root=tmp_path / "runs",
        resources=ResourceCoordinator(),
    )
    try:
        cancel = threading.Event()
        with bound_context(cancel_event=cancel):
            dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
            task_ids = dispatched.data["task_ids"]
            threading.Timer(0.2, cancel.set).start()
            result = AwaitSubagentsTool(manager).execute(task_ids=task_ids, timeout_s=30)
        assert result.ok is False
        assert result.error_category == "user_cancelled"
    finally:
        block.set()
        manager.shutdown()
        store.close()


# ─── control ─────────────────────────────────────────────────────────────────


def test_control_allowlists_actions(manager) -> None:
    with bound_context():
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_id = dispatched.data["task_ids"][0]
        AwaitSubagentsTool(manager).execute(task_ids=[task_id], timeout_s=10)
        result = ControlSubagentTool(manager).execute(task_id=task_id, action="delete")
    assert result.ok is False
    assert result.code == 400


def test_control_continue_requires_instruction(manager) -> None:
    with bound_context():
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_id = dispatched.data["task_ids"][0]
        AwaitSubagentsTool(manager).execute(task_ids=[task_id], timeout_s=10)
        missing = ControlSubagentTool(manager).execute(task_id=task_id, action="continue")
        ok = ControlSubagentTool(manager).execute(
            task_id=task_id, action="continue", instruction="再对比一下最新政策"
        )
    assert missing.ok is False and missing.code == 400
    assert ok.ok is True
    assert ok.data["task"]["attempt"] == 2
    assert ok.data["task"]["status"] in {"queued", "running", "completed"}


def test_control_retry_after_cancel(manager) -> None:
    with bound_context():
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_id = dispatched.data["task_ids"][0]
        AwaitSubagentsTool(manager).execute(task_ids=[task_id], timeout_s=10)
        # completed → retry invalid
        invalid = ControlSubagentTool(manager).execute(task_id=task_id, action="retry")
    assert invalid.ok is False
    assert invalid.code == 400


def test_control_rejects_foreign_and_unknown_tasks(manager) -> None:
    with bound_context("conv-a"):
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_id = dispatched.data["task_ids"][0]
    with bound_context("conv-b"):
        foreign = ControlSubagentTool(manager).execute(task_id=task_id, action="cancel")
        unknown = ControlSubagentTool(manager).execute(task_id="missing", action="cancel")
    assert foreign.error_category == "task_not_found"
    assert unknown.error_category == "task_not_found"
    # Same opaque message for both (existence not observable).
    assert foreign.error == unknown.error


def test_control_requires_context(manager) -> None:
    result = ControlSubagentTool(manager).execute(task_id="t1", action="cancel")
    assert result.error_category == "missing_execution_context"


def test_cancel_completed_task_is_noop_like(manager) -> None:
    with bound_context():
        dispatched = DispatchSubagentsTool(manager).execute(tasks=[task_item()])
        task_id = dispatched.data["task_ids"][0]
        AwaitSubagentsTool(manager).execute(task_ids=[task_id], timeout_s=10)
        result = ControlSubagentTool(manager).execute(task_id=task_id, action="cancel")
    assert result.ok is True
    assert result.data["task"]["status"] == SubagentStatus.COMPLETED.value


def test_tools_have_nested_schemas_for_openai(manager) -> None:
    dispatch_schema = DispatchSubagentsTool(manager).to_openai_schema()
    tasks_prop = dispatch_schema["function"]["parameters"]["properties"]["tasks"]
    assert tasks_prop["type"] == "array"
    assert tasks_prop["items"]["required"] == ["title", "role", "goal"]
    assert set(tasks_prop["items"]["properties"]) == {"title", "role", "goal", "context"}
    assert tasks_prop["maxItems"] == 4
    roles = tasks_prop["items"]["properties"]["role"]["enum"]
    assert set(roles) == {role.value for role in SubagentRole}

    await_schema = AwaitSubagentsTool(manager).to_openai_schema()
    ids_prop = await_schema["function"]["parameters"]["properties"]["task_ids"]
    assert ids_prop["items"] == {"type": "string"}
