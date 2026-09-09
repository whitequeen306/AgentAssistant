"""Bridge wiring for subagent tasks: init hydration + controls (Task 10)."""

from __future__ import annotations

import threading

import pytest

from agent_assistant.subagents.manager import SubagentManager
from agent_assistant.subagents.models import (
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)
from agent_assistant.subagents.resources import ResourceCoordinator
from agent_assistant.subagents.store import SubagentStore
from agent_assistant.ui.bridge import ApiBridge


def make_spec(task_id: str, conversation_id: str = "conv-1") -> SubagentSpec:
    return SubagentSpec(
        task_id=task_id,
        conversation_id=conversation_id,
        parent_turn_id="turn-1",
        role=SubagentRole.EXPLORER,
        title=f"任务 {task_id}",
        goal="找到考研资料",
    )


def instant_runner(spec, controls, emit):
    emit("activity", {"label": "扫描 Documents"})
    return SubagentResult.completed(
        task_id=spec.task_id,
        role=spec.role,
        attempt=spec.attempt,
        summary="找到 3 个文件",
        output="详见列表",
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


@pytest.fixture()
def bridge(manager):
    api = ApiBridge()
    api._active_conv_id = "conv-1"
    api.set_subagent_manager(manager)
    return api


def _run_one(manager, task_id: str, conversation_id: str = "conv-1") -> None:
    manager.dispatch([make_spec(task_id, conversation_id)])
    results = manager.await_tasks([task_id], timeout=5)
    assert len(results) == 1


def test_list_subagent_tasks_returns_only_active_conversation(bridge, manager) -> None:
    _run_one(manager, "t1", "conv-1")
    _run_one(manager, "t2", "conv-2")

    views = bridge.list_subagent_tasks()
    assert [view["task_id"] for view in views] == ["t1"]
    view = views[0]
    assert view["status"] == "completed"
    assert view["sequence"] >= 1
    assert view["result"]["summary"] == "找到 3 个文件"
    assert any(event["type"] == "activity" for event in view["events"])


def test_init_data_includes_subagents(bridge, manager, monkeypatch, tmp_path) -> None:
    from agent_assistant.ui import store as store_mod

    db = tmp_path / "ui.db"
    ui = store_mod.UIStore(db_path=db)
    monkeypatch.setattr(store_mod, "ui_store", ui)
    conv = ui.create_conversation()
    bridge._active_conv_id = conv["id"]

    _run_one(manager, "t1", conv["id"])

    class FakeWindow:
        current_state = "main"

    monkeypatch.setattr("agent_assistant.ui.window.ui_window", FakeWindow())
    data = bridge.get_init_data()
    assert "subagents" in data
    assert [view["task_id"] for view in data["subagents"]] == ["t1"]


def test_control_subagent_refuses_foreign_conversation(bridge, manager) -> None:
    _run_one(manager, "t2", "conv-2")
    response = bridge.control_subagent("t2", "cancel")
    assert response["ok"] is False
    assert "任务" in response["error"]


def test_control_subagent_continue_and_invalid_action(bridge, manager) -> None:
    _run_one(manager, "t1", "conv-1")
    bad = bridge.control_subagent("t1", "delete")
    assert bad["ok"] is False
    ok = bridge.control_subagent("t1", "continue", "再补充一个来源")
    assert ok["ok"] is True
    assert ok["task"]["attempt"] == 2
    manager.await_tasks(["t1"], timeout=5)


def test_respond_organizer_plan_wrong_role_until_organizer_lands(bridge, manager) -> None:
    _run_one(manager, "t1", "conv-1")
    response = bridge.respond_organizer_plan("t1", "hash", True)
    assert response["ok"] is False
    assert response.get("error_category") == "wrong_role"


def test_manager_events_are_pushed_with_task_id_and_sequence(tmp_path) -> None:
    """The launch sink must survive _push_event's {"type": outer, **data} merge."""
    store = SubagentStore(tmp_path / "subagents.db")
    pushed: list[dict] = []

    class FakeBridge:
        def push_event(self, event_type, payload):
            # Same merge semantics as ApiBridge._push_event.
            pushed.append({"type": event_type, **payload})

    fake_bridge = FakeBridge()

    def sink(event):
        # Mirror launch.py's on_subagent_event exactly.
        data = event.to_dict()
        data["event_type"] = data.pop("type")
        fake_bridge.push_event("subagent_event", data)

    manager = SubagentManager(
        store,
        runner_for=lambda role: instant_runner,
        event_sink=sink,
        archive_root=tmp_path / "runs",
        resources=ResourceCoordinator(),
    )
    try:
        manager.dispatch([make_spec("t1")])
        manager.await_tasks(["t1"], timeout=5)
        assert pushed
        for payload in pushed:
            assert payload["type"] == "subagent_event"  # envelope intact
            assert payload["task_id"] == "t1"
            assert payload["conversation_id"] == "conv-1"
            assert payload["sequence"] >= 1
        sequences = [payload["sequence"] for payload in pushed]
        assert sequences == sorted(sequences)
        assert pushed[-1]["event_type"] == "result"
    finally:
        manager.shutdown()
        store.close()


def test_no_manager_bound_is_safe(monkeypatch) -> None:
    api = ApiBridge()
    api._active_conv_id = "conv-1"
    assert api.list_subagent_tasks() == []
    assert api.control_subagent("t1", "cancel")["ok"] is False
    assert api.respond_organizer_plan("t1", "h", True)["ok"] is False


def test_recover_on_startup_marks_tasks_interrupted(tmp_path, monkeypatch) -> None:
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import service

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    service.reset_for_tests()
    try:
        seed = SubagentStore(app_settings.subagent_db_path)
        seed.create_task(
            SubagentSpec(
                task_id="old1",
                conversation_id="conv-1",
                parent_turn_id="turn-1",
                role=SubagentRole.RESEARCHER,
                title="上次的调研",
                goal="重启前的任务",
                status=SubagentStatus.RUNNING,
            )
        )
        seed.close()

        events: list = []
        service.set_event_sink(events.append)
        recovered = service.recover_on_startup()
        assert recovered == 1
        task = service.get_manager().get_task("old1")
        assert task.status is SubagentStatus.INTERRUPTED
        assert task.previous_status is SubagentStatus.RUNNING
        assert any(
            event.payload.get("status") == "interrupted" for event in events
        )
    finally:
        service.reset_for_tests()


def test_control_uses_lock_free_snapshot_thread_safety(bridge, manager) -> None:
    """Concurrent UI control calls must not corrupt manager state."""
    _run_one(manager, "t1", "conv-1")
    responses: list[dict] = []

    def hammer() -> None:
        responses.append(bridge.control_subagent("t1", "cancel"))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(responses) == 8
    # Task stays terminal (completed) — cancel on terminal is a no-op-like ok.
    assert manager.get_task("t1").status is SubagentStatus.COMPLETED
