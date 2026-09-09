"""Desktop / filesystem-write resource ownership (Task 5)."""

from __future__ import annotations

import threading

import pytest

from agent_assistant.subagents.context import AgentExecutionContext, bind_execution_context
from agent_assistant.subagents.resources import (
    DESKTOP,
    FILESYSTEM_WRITE,
    ResourceBusyError,
    ResourceCoordinator,
    resource_for_tool,
)
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.permission import PermissionPolicy
from agent_assistant.tools.registry import ToolRegistry


def test_only_one_desktop_owner() -> None:
    resources = ResourceCoordinator()
    first = resources.try_acquire(DESKTOP, "task-a")
    assert first is not None
    assert resources.try_acquire(DESKTOP, "task-b") is None
    first.release()
    assert resources.try_acquire(DESKTOP, "task-b") is not None


def test_resources_are_independent() -> None:
    resources = ResourceCoordinator()
    assert resources.try_acquire(DESKTOP, "task-a") is not None
    assert resources.try_acquire(FILESYSTEM_WRITE, "task-b") is not None


def test_reentrant_acquire_needs_matching_releases() -> None:
    resources = ResourceCoordinator()
    first = resources.try_acquire(DESKTOP, "task-a")
    second = resources.try_acquire(DESKTOP, "task-a")
    assert first is not None and second is not None
    first.release()
    assert resources.owner(DESKTOP) == "task-a"
    second.release()
    assert resources.owner(DESKTOP) is None


def test_release_is_idempotent_and_epoch_safe() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    lease.release()
    lease.release()  # no-op
    # New epoch: task-b owns; a stale release from task-a must not free it.
    fresh = resources.try_acquire(DESKTOP, "task-b")
    assert fresh is not None
    lease.release()
    assert resources.owner(DESKTOP) == "task-b"


def test_same_owner_can_execute_guarded_tool() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    with resources.guard_tool("ui_click", "task-a"):
        assert resources.owner(DESKTOP) == "task-a"
    # Owner pass-through must not release the task's hold.
    assert resources.owner(DESKTOP) == "task-a"
    lease.release()


def test_guard_tool_ephemeral_acquire_when_unowned() -> None:
    resources = ResourceCoordinator()
    with resources.guard_tool("ui_click", "main:conv:turn:n1"):
        assert resources.owner(DESKTOP) == "main:conv:turn:n1"
    assert resources.owner(DESKTOP) is None
    # Anonymous caller (no execution context) also works.
    with resources.guard_tool("move_file", None):
        assert resources.owner(FILESYSTEM_WRITE) is not None
    assert resources.owner(FILESYSTEM_WRITE) is None


def test_guard_tool_raises_busy_for_foreign_owner() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    with pytest.raises(ResourceBusyError):
        with resources.guard_tool("ui_click", "main:conv:turn:n1"):
            pass
    lease.release()


def test_guard_tool_ignores_unguarded_tools() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    with resources.guard_tool("web_search", "someone-else"):
        pass  # no resource involved
    lease.release()


def test_blocking_acquire_waits_for_release() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    acquired = threading.Event()

    def waiter() -> None:
        got = resources.acquire(DESKTOP, "task-b", timeout=5)
        acquired.set()
        got.release()

    thread = threading.Thread(target=waiter)
    thread.start()
    assert not acquired.wait(timeout=0.3)
    lease.release()
    assert acquired.wait(timeout=5)
    thread.join(timeout=5)
    assert resources.owner(DESKTOP) is None


def test_blocking_acquire_timeout_raises_busy() -> None:
    resources = ResourceCoordinator()
    lease = resources.try_acquire(DESKTOP, "task-a")
    assert lease is not None
    with pytest.raises(ResourceBusyError):
        resources.acquire(DESKTOP, "task-b", timeout=0.3)
    lease.release()


def test_release_all_frees_every_resource_of_owner() -> None:
    resources = ResourceCoordinator()
    resources.try_acquire(DESKTOP, "task-a")
    resources.try_acquire(FILESYSTEM_WRITE, "task-a")
    resources.release_all("task-a")
    assert resources.owner(DESKTOP) is None
    assert resources.owner(FILESYSTEM_WRITE) is None


def test_resource_for_tool_mapping() -> None:
    assert resource_for_tool("ui_type") == DESKTOP
    assert resource_for_tool("launch_app") == DESKTOP
    assert resource_for_tool("move_file") == FILESYSTEM_WRITE
    assert resource_for_tool("save_note") == FILESYSTEM_WRITE
    assert resource_for_tool("web_search") is None


def test_invalid_inputs_rejected() -> None:
    resources = ResourceCoordinator()
    with pytest.raises(ValueError):
        resources.try_acquire("gpu", "task-a")
    with pytest.raises(ValueError):
        resources.try_acquire(DESKTOP, "  ")


# ─── ToolRegistry integration ─────────────────────────────────────────────────


class _FakeUiClick(Tool):
    @property
    def name(self) -> str:
        return "ui_click"

    @property
    def description(self) -> str:
        return "fake desktop click"

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult.success(data={"clicked": True})


def _allow_all_registry() -> ToolRegistry:
    registry = ToolRegistry(permission_policy=PermissionPolicy(rules=[]))
    registry.register(_FakeUiClick())
    return registry


def test_main_ui_tool_fails_when_operator_owns_desktop(monkeypatch) -> None:
    import agent_assistant.tools.registry as registry_module
    from agent_assistant.subagents import resources as resources_module

    coordinator = ResourceCoordinator()
    monkeypatch.setattr(resources_module, "resource_coordinator", coordinator)
    registry = _allow_all_registry()

    lease = coordinator.try_acquire(DESKTOP, "subagent:operator-task")
    assert lease is not None
    result = registry.execute("ui_click", {})
    assert result.ok is False
    assert result.error_category == "resource_busy"
    assert result.code == 423
    lease.release()

    # After release, the same call succeeds via ephemeral acquire.
    result = registry.execute("ui_click", {})
    assert result.ok is True
    assert registry_module is not None  # keep import referenced


def test_owning_subagent_context_passes_registry_guard(monkeypatch) -> None:
    from agent_assistant.subagents import resources as resources_module

    coordinator = ResourceCoordinator()
    monkeypatch.setattr(resources_module, "resource_coordinator", coordinator)
    registry = _allow_all_registry()

    lease = coordinator.try_acquire(DESKTOP, "subagent:op-1")
    assert lease is not None
    context = AgentExecutionContext(
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        owner_id="subagent:op-1",
        cancel_event=threading.Event(),
    )
    with bind_execution_context(context):
        result = registry.execute("ui_click", {})
    assert result.ok is True
    assert coordinator.owner(DESKTOP) == "subagent:op-1"
    lease.release()
