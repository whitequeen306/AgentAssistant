"""Global desktop / filesystem-write resource ownership.

Two singleton resources coordinate the main agent and subagents:

- ``desktop``: foreground UI automation (Operator holds it for a whole task).
- ``filesystem_write``: batch file mutations (Organizer holds it during apply).

Ownership is tracked by owner ID under one mutex — NOT by ``threading.Lock``
ownership — because acquire and release may happen on different threads
(scheduler vs worker) and holds span many tool calls.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Iterator

DESKTOP = "desktop"
FILESYSTEM_WRITE = "filesystem_write"
_RESOURCES = frozenset({DESKTOP, FILESYSTEM_WRITE})

# Tools guarded by each resource. Main-agent calls acquire ephemerally; a
# subagent that owns the resource passes through; everyone else gets busy.
DESKTOP_TOOLS = frozenset(
    {
        "launch_app",
        "focus_window",
        "ui_inspect",
        "ui_click",
        "ui_type",
        "ui_hotkey",
        "ui_scroll",
    }
)
FILESYSTEM_WRITE_TOOLS = frozenset({"move_file", "write_file", "edit_file", "save_note"})


class ResourceBusyError(RuntimeError):
    """The resource is exclusively owned by another task."""

    def __init__(self, resource: str, owner_id: str) -> None:
        super().__init__(f"resource '{resource}' is busy")
        self.resource = resource
        self.owner_id = owner_id


def resource_for_tool(tool_name: str) -> str | None:
    """Which resource (if any) a tool call needs."""
    if tool_name in DESKTOP_TOOLS:
        return DESKTOP
    if tool_name in FILESYSTEM_WRITE_TOOLS:
        return FILESYSTEM_WRITE
    return None


class ResourceLease:
    """Handle for one acquisition; ``release()`` is idempotent."""

    def __init__(self, coordinator: ResourceCoordinator, resource: str, owner_id: str) -> None:
        self._coordinator = coordinator
        self.resource = resource
        self.owner_id = owner_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._coordinator._release(self.resource, self.owner_id)

    def __enter__(self) -> ResourceLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ResourceCoordinator:
    """Single-owner, hold-counted coordinator for the global resources."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._condition = threading.Condition(self._mutex)
        self._owners: dict[str, str] = {}
        self._hold_counts: dict[str, int] = {}

    def owner(self, resource: str) -> str | None:
        self._validate(resource)
        with self._mutex:
            return self._owners.get(resource)

    def try_acquire(self, resource: str, owner_id: str) -> ResourceLease | None:
        """Acquire without blocking; re-entrant for the same owner."""
        self._validate(resource)
        self._validate_owner(owner_id)
        with self._mutex:
            current = self._owners.get(resource)
            if current is None:
                self._owners[resource] = owner_id
                self._hold_counts[resource] = 1
                return ResourceLease(self, resource, owner_id)
            if current == owner_id:
                self._hold_counts[resource] += 1
                return ResourceLease(self, resource, owner_id)
            return None

    def acquire(
        self,
        resource: str,
        owner_id: str,
        timeout: float | None = None,
        cancel_check: object | None = None,
    ) -> ResourceLease:
        """Blocking acquire with optional timeout and cancel polling.

        ``cancel_check`` is an optional zero-arg callable polled while waiting;
        when it returns True the wait aborts with ``ResourceBusyError``.
        """
        self._validate(resource)
        self._validate_owner(owner_id)
        deadline = None if timeout is None else max(0.0, timeout)
        with self._condition:
            waited = 0.0
            while True:
                current = self._owners.get(resource)
                if current is None or current == owner_id:
                    self._owners[resource] = owner_id
                    self._hold_counts[resource] = self._hold_counts.get(resource, 0) + 1
                    return ResourceLease(self, resource, owner_id)
                if callable(cancel_check) and cancel_check():
                    raise ResourceBusyError(resource, current)
                if deadline is not None and waited >= deadline:
                    raise ResourceBusyError(resource, current)
                step = 0.2 if deadline is None else min(0.2, deadline - waited)
                self._condition.wait(timeout=step)
                waited += step

    def _release(self, resource: str, owner_id: str) -> None:
        with self._condition:
            if self._owners.get(resource) != owner_id:
                return  # stale lease from a previous ownership epoch
            remaining = self._hold_counts.get(resource, 1) - 1
            if remaining <= 0:
                self._owners.pop(resource, None)
                self._hold_counts.pop(resource, None)
                self._condition.notify_all()
            else:
                self._hold_counts[resource] = remaining

    def release_all(self, owner_id: str) -> None:
        """Force-release every resource held by ``owner_id`` (task teardown)."""
        with self._condition:
            for resource in [r for r, o in self._owners.items() if o == owner_id]:
                self._owners.pop(resource, None)
                self._hold_counts.pop(resource, None)
            self._condition.notify_all()

    @contextmanager
    def guard_tool(self, tool_name: str, owner_id: str | None = None) -> Iterator[None]:
        """Gate one tool call on its resource.

        - Tool needs no resource → pass through.
        - ``owner_id`` already owns it → pass through (no acquire/release).
        - Resource unowned → ephemeral acquire for the duration of the call.
        - Owned by someone else → raise ``ResourceBusyError``.
        """
        resource = resource_for_tool(tool_name)
        if resource is None:
            yield
            return
        ephemeral: ResourceLease | None = None
        with self._mutex:
            current = self._owners.get(resource)
            if current is not None and owner_id is not None and current == owner_id:
                pass  # owner passes through without touching hold counts
            elif current is None:
                caller = owner_id or f"ephemeral:{uuid.uuid4().hex}"
                self._owners[resource] = caller
                self._hold_counts[resource] = 1
                ephemeral = ResourceLease(self, resource, caller)
            else:
                raise ResourceBusyError(resource, current)
        try:
            yield
        finally:
            if ephemeral is not None:
                ephemeral.release()

    @staticmethod
    def _validate(resource: str) -> None:
        if resource not in _RESOURCES:
            raise ValueError("unknown resource")

    @staticmethod
    def _validate_owner(owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")


# Process-wide singleton used by the tool registry and the subagent manager.
resource_coordinator = ResourceCoordinator()
