"""SubagentManager scheduling, control, events, recovery (Task 6)."""

from __future__ import annotations

import threading
import time

import pytest

from agent_assistant.subagents.manager import (
    PausedOutcome,
    SubagentManager,
    TaskControls,
    task_owner_id,
)
from agent_assistant.subagents.models import (
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)
from agent_assistant.subagents.resources import DESKTOP, ResourceCoordinator
from agent_assistant.subagents.store import SubagentStore


def make_spec(task_id: str, role: SubagentRole = SubagentRole.EXPLORER) -> SubagentSpec:
    return SubagentSpec(
        task_id=task_id,
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=role,
        title=f"任务 {task_id}",
        goal=f"目标 {task_id}",
    )


def completed(spec: SubagentSpec, output: str = "done") -> SubagentResult:
    return SubagentResult.completed(
        task_id=spec.task_id,
        role=spec.role,
        attempt=spec.attempt,
        summary=f"完成 {spec.task_id}",
        output=output,
    )


@pytest.fixture()
def store(tmp_path) -> SubagentStore:
    s = SubagentStore(tmp_path / "subagents.db")
    yield s
    s.close()


def build_manager(store, runner, tmp_path, *, resources=None, max_workers=4, sink=None):
    return SubagentManager(
        store,
        runner_for=lambda role: runner,
        event_sink=sink,
        archive_root=tmp_path / "runs",
        max_workers=max_workers,
        resources=resources or ResourceCoordinator(),
    )


def test_dispatch_persists_before_queueing(store, tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(spec, controls, emit):
        started.set()
        release.wait(timeout=5)
        return completed(spec)

    manager = build_manager(store, runner, tmp_path)
    try:
        ids = manager.dispatch([make_spec("t1")])
        assert ids == ["t1"]
        persisted = store.get_task("t1")
        assert persisted is not None
        assert persisted.status in {SubagentStatus.QUEUED, SubagentStatus.RUNNING}
        release.set()
        results = manager.await_tasks(["t1"], timeout=5)
        assert len(results) == 1 and results[0].status is SubagentStatus.COMPLETED
        assert store.get_task("t1").status is SubagentStatus.COMPLETED
    finally:
        release.set()
        manager.shutdown()


def test_four_run_concurrently_and_fifth_waits(store, tmp_path) -> None:
    concurrent = 0
    peak = 0
    mutex = threading.Lock()
    release = threading.Event()

    def runner(spec, controls, emit):
        nonlocal concurrent, peak
        with mutex:
            concurrent += 1
            peak = max(peak, concurrent)
        release.wait(timeout=10)
        with mutex:
            concurrent -= 1
        return completed(spec)

    manager = build_manager(store, runner, tmp_path, max_workers=4)
    try:
        ids = manager.dispatch([make_spec(f"t{i}") for i in range(5)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with mutex:
                if concurrent == 4:
                    break
            time.sleep(0.02)
        with mutex:
            assert concurrent == 4  # fifth is waiting for a slot
        release.set()
        results = manager.await_tasks(ids, timeout=10)
        assert len(results) == 5
        assert peak == 4
    finally:
        release.set()
        manager.shutdown()


def test_await_wakes_on_completion_event(store, tmp_path) -> None:
    gate = threading.Event()

    def runner(spec, controls, emit):
        gate.wait(timeout=5)
        return completed(spec)

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        timer = threading.Timer(0.15, gate.set)
        timer.start()
        t0 = time.monotonic()
        results = manager.await_tasks(["t1"], timeout=10)
        elapsed = time.monotonic() - t0
        assert len(results) == 1
        assert elapsed < 5  # woke via notify, not full timeout
    finally:
        gate.set()
        manager.shutdown()


def test_await_respects_parent_cancel_check(store, tmp_path) -> None:
    def runner(spec, controls, emit):
        controls.cancel_event.wait(timeout=10)
        return completed(spec)

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        cancelled = threading.Event()
        threading.Timer(0.2, cancelled.set).start()
        t0 = time.monotonic()
        results = manager.await_tasks(["t1"], timeout=10, cancel_check=cancelled.is_set)
        assert results == []
        assert time.monotonic() - t0 < 5
    finally:
        manager.control("t1", action="cancel")
        manager.shutdown()


def test_event_sequences_are_persisted_and_monotonic(store, tmp_path) -> None:
    seen: list[tuple[str, int]] = []

    def sink(event):
        seen.append((event.type, event.sequence))

    def runner(spec, controls, emit):
        emit("activity", {"label": "第一步"})
        emit("activity", {"label": "第二步"})
        return completed(spec)

    manager = build_manager(store, runner, tmp_path, sink=sink)
    try:
        manager.dispatch([make_spec("t1")])
        manager.await_tasks(["t1"], timeout=5)
        sequences = [sequence for _, sequence in seen]
        assert sequences == sorted(sequences)
        assert manager.last_sequence("t1") == max(sequences)
        stored = store.list_events("t1")
        assert [event.sequence for event in stored] == list(range(1, len(stored) + 1))
        # Persist-before-publish: everything the sink saw is in the store.
        assert len(stored) >= len(seen)
    finally:
        manager.shutdown()


def test_cancel_running_task_preserves_partial_result(store, tmp_path) -> None:
    started = threading.Event()

    def runner(spec, controls, emit):
        started.set()
        controls.cancel_event.wait(timeout=10)
        return SubagentResult.cancelled(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="已取消，返回部分进度",
            partial_result={"visited": ["a", "b"]},
        )

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        assert started.wait(timeout=5)
        manager.control("t1", action="cancel")
        results = manager.await_tasks(["t1"], timeout=5)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.CANCELLED
        assert results[0].partial_result is not None
    finally:
        manager.shutdown()


def test_cancel_queued_task_synthesizes_cancelled_result(store, tmp_path) -> None:
    release = threading.Event()

    def runner(spec, controls, emit):
        release.wait(timeout=10)
        return completed(spec)

    # One worker: t1 occupies it, t2 stays queued.
    manager = build_manager(store, runner, tmp_path, max_workers=1)
    try:
        manager.dispatch([make_spec("t1"), make_spec("t2")])
        manager.control("t2", action="cancel")
        results = manager.await_tasks(["t2"], timeout=5)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.CANCELLED
        assert results[0].partial_result is not None
        release.set()
        manager.await_tasks(["t1"], timeout=5)
    finally:
        release.set()
        manager.shutdown()


def test_desktop_tasks_serialize_but_do_not_starve_workers(store, tmp_path) -> None:
    resources = ResourceCoordinator()
    desktop_running = 0
    desktop_peak = 0
    mutex = threading.Lock()
    hold_first = threading.Event()

    def runner(spec, controls, emit):
        nonlocal desktop_running, desktop_peak
        if spec.role is SubagentRole.OPERATOR:
            with mutex:
                desktop_running += 1
                desktop_peak = max(desktop_peak, desktop_running)
            if spec.task_id == "op1":
                hold_first.wait(timeout=10)
            with mutex:
                desktop_running -= 1
        return completed(spec)

    manager = build_manager(store, runner, tmp_path, resources=resources, max_workers=4)
    try:
        manager.dispatch(
            [
                make_spec("op1", role=SubagentRole.OPERATOR),
                make_spec("op2", role=SubagentRole.OPERATOR),
                make_spec("e1"),
                make_spec("e2"),
                make_spec("e3"),
            ]
        )
        # Explorers finish while op1 holds the desktop and op2 waits for it —
        # the desktop waiter must not consume a worker slot.
        explorer_results = manager.await_tasks(["e1", "e2", "e3"], timeout=5)
        assert len(explorer_results) == 3
        assert resources.owner(DESKTOP) == task_owner_id("op1")
        hold_first.set()
        results = manager.await_tasks(["op1", "op2"], timeout=10)
        assert len(results) == 2
        assert desktop_peak == 1
        assert resources.owner(DESKTOP) is None
    finally:
        hold_first.set()
        manager.shutdown()


def test_pause_checkpoints_releases_desktop_and_resume_completes(store, tmp_path) -> None:
    resources = ResourceCoordinator()
    phase = {"count": 0}

    def runner(spec, controls, emit):
        phase["count"] += 1
        if phase["count"] == 1:
            # Wait for the pause request, then yield at the safe boundary.
            assert controls.pause_event.wait(timeout=5)
            return PausedOutcome(checkpoint={"step": 3, "note": "safe point"})
        return completed(spec, output="resumed and finished")

    manager = build_manager(store, runner, tmp_path, resources=resources)
    try:
        manager.dispatch([make_spec("op1", role=SubagentRole.OPERATOR)])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = manager.get_task("op1")
            if task is not None and task.status is SubagentStatus.RUNNING:
                break
            time.sleep(0.02)
        manager.control("op1", action="pause")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if manager.get_task("op1").status is SubagentStatus.PAUSED:
                break
            time.sleep(0.02)
        assert manager.get_task("op1").status is SubagentStatus.PAUSED
        assert resources.owner(DESKTOP) is None  # lock released while paused
        assert store.get_task("op1").status is SubagentStatus.PAUSED

        manager.control("op1", action="resume")
        results = manager.await_tasks(["op1"], timeout=10)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.COMPLETED
        assert results[0].attempt == 1  # resume keeps the attempt
    finally:
        manager.shutdown()


def test_retry_increments_attempt_and_keeps_task_id(store, tmp_path) -> None:
    calls = {"count": 0}

    def runner(spec, controls, emit):
        calls["count"] += 1
        if calls["count"] == 1:
            return SubagentResult.failure(
                task_id=spec.task_id,
                role=spec.role,
                attempt=spec.attempt,
                summary="第一次失败",
                error_category="unknown",
                error="synthetic failure",
            )
        return completed(spec)

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        first = manager.await_tasks(["t1"], timeout=5)
        assert first[0].status is SubagentStatus.FAILED

        manager.control("t1", action="retry")
        second = manager.await_tasks(["t1"], timeout=5)
        assert second[0].status is SubagentStatus.COMPLETED
        assert second[0].attempt == 2
        assert store.get_task("t1").attempt == 2
    finally:
        manager.shutdown()


def test_continue_requires_instruction_and_passes_context(store, tmp_path) -> None:
    seen_context: list[dict] = []

    def runner(spec, controls, emit):
        seen_context.append(dict(spec.context))
        return completed(spec)

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        manager.await_tasks(["t1"], timeout=5)

        with pytest.raises(ValueError):
            manager.control("t1", action="continue", instruction="  ")

        manager.control("t1", action="continue", instruction="补充调查昨天的数据")
        results = manager.await_tasks(["t1"], timeout=5)
        assert results[0].attempt == 2
        assert seen_context[-1].get("continue_instruction") == "补充调查昨天的数据"
    finally:
        manager.shutdown()


def test_control_rejects_unknown_action_and_unknown_task(store, tmp_path) -> None:
    manager = build_manager(store, lambda *a: None, tmp_path)
    try:
        with pytest.raises(ValueError):
            manager.control("t1", action="delete")
        with pytest.raises(KeyError):
            manager.control("missing", action="cancel")
    finally:
        manager.shutdown()


def test_runner_exception_becomes_sanitized_failure(store, tmp_path) -> None:
    def runner(spec, controls, emit):
        raise RuntimeError(r"C:\Users\hp\secret\path failed with token=abc123")

    manager = build_manager(store, runner, tmp_path)
    try:
        manager.dispatch([make_spec("t1")])
        results = manager.await_tasks(["t1"], timeout=5)
        assert results[0].status is SubagentStatus.FAILED
        assert "C:\\Users" not in (results[0].error or "")
        assert "abc123" not in (results[0].error or "")
    finally:
        manager.shutdown()


def test_recover_marks_persisted_nonterminal_tasks_interrupted(tmp_path) -> None:
    db = tmp_path / "subagents.db"
    seed = SubagentStore(db)
    for index, status in enumerate(
        [
            SubagentStatus.QUEUED,
            SubagentStatus.RUNNING,
            SubagentStatus.WAITING_USER,
            SubagentStatus.PAUSED,
            SubagentStatus.COMPLETED,
        ]
    ):
        seed.create_task(
            SubagentSpec(
                task_id=f"t{index}",
                conversation_id="conv-1",
                parent_turn_id="turn-1",
                role=SubagentRole.EXPLORER,
                title="旧任务",
                goal="重启前的任务",
                status=status,
            )
        )
    seed.close()

    store = SubagentStore(db)
    events: list = []
    manager = SubagentManager(
        store,
        runner_for=lambda role: (lambda *a: None),
        event_sink=events.append,
        archive_root=tmp_path / "runs",
    )
    try:
        recovered = manager.recover()
        assert {spec.task_id for spec in recovered} == {"t0", "t1", "t2", "t3"}
        assert all(spec.status is SubagentStatus.INTERRUPTED for spec in recovered)
        assert {spec.previous_status for spec in recovered} == {
            SubagentStatus.QUEUED,
            SubagentStatus.RUNNING,
            SubagentStatus.WAITING_USER,
            SubagentStatus.PAUSED,
        }
        assert store.get_task("t4").status is SubagentStatus.COMPLETED
        interrupted_events = [e for e in events if e.payload.get("status") == "interrupted"]
        assert len(interrupted_events) == 4
        # No task resumed automatically.
        assert manager.result_for("t0") is None
    finally:
        manager.shutdown()
        store.close()


def test_interrupted_task_can_be_retried_after_restart(tmp_path) -> None:
    """Restart → interrupted → retry runs again with attempt+1 and a fresh queue."""
    db = tmp_path / "subagents.db"
    seed = SubagentStore(db)
    seed.create_task(
        SubagentSpec(
            task_id="t1",
            conversation_id="conv-1",
            parent_turn_id="turn-1",
            role=SubagentRole.EXPLORER,
            title="重启前的任务",
            goal="继续找资料",
            status=SubagentStatus.RUNNING,
        )
    )
    seed.close()

    store = SubagentStore(db)
    manager = SubagentManager(
        store,
        runner_for=lambda role: (lambda spec, controls, emit: completed(spec)),
        archive_root=tmp_path / "runs",
    )
    try:
        recovered = manager.recover()
        assert [spec.task_id for spec in recovered] == ["t1"]
        assert manager.result_for("t1") is None  # no auto-resume

        manager.control("t1", action="retry")
        results = manager.await_tasks(["t1"], timeout=5)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.COMPLETED
        assert results[0].attempt == 2
        # Event sequences continued monotonically across the restart.
        events = store.list_events("t1")
        sequences = [event.sequence for event in events]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))
    finally:
        manager.shutdown()
        store.close()


def test_dispatch_unregistered_role_fails_fast(store, tmp_path) -> None:
    from agent_assistant.subagents.service import RunnerNotRegisteredError

    def runner_for(role):
        raise RunnerNotRegisteredError(f"no runner for {role}")

    manager = SubagentManager(
        store,
        runner_for=runner_for,
        archive_root=tmp_path / "runs",
    )
    try:
        with pytest.raises(RunnerNotRegisteredError):
            manager.dispatch([make_spec("t1")])
        assert store.get_task("t1") is None  # nothing persisted
    finally:
        manager.shutdown()


def test_task_controls_should_yield() -> None:
    controls = TaskControls("t1", threading.Event(), threading.Event())
    assert controls.should_yield() is False
    controls.pause_event.set()
    assert controls.should_yield() is True
    controls.pause_event.clear()
    controls.cancel_event.set()
    assert controls.should_yield() is True
