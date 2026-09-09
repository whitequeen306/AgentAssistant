"""Per-turn execution context shared by main-agent tool calls."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


class MissingExecutionContext(RuntimeError):  # noqa: N818 - required public API
    """Raised when code requiring a bound agent turn runs outside one."""


@dataclass(frozen=True)
class AgentExecutionContext:
    """Identity and cancellation state for one main-agent turn."""

    conversation_id: str
    parent_turn_id: str
    owner_id: str
    cancel_event: threading.Event

    def __post_init__(self) -> None:
        for field_name in ("conversation_id", "parent_turn_id", "owner_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.cancel_event, threading.Event):
            raise TypeError("cancel_event must be a threading.Event")


_execution_context: ContextVar[AgentExecutionContext | None] = ContextVar(
    "agent_execution_context",
    default=None,
)


@contextmanager
def bind_execution_context(
    context: AgentExecutionContext,
) -> Iterator[AgentExecutionContext]:
    """Bind a turn context and restore the previous value on exit."""
    if not isinstance(context, AgentExecutionContext):
        raise TypeError("context must be an AgentExecutionContext")
    token = _execution_context.set(context)
    try:
        yield context
    finally:
        _execution_context.reset(token)


def current_execution_context() -> AgentExecutionContext | None:
    """Return the current turn context, if one is bound."""
    return _execution_context.get()


def require_execution_context() -> AgentExecutionContext:
    """Return the current context or fail with a stable public error."""
    context = current_execution_context()
    if context is None:
        raise MissingExecutionContext(
            "agent execution context is required for this operation"
        )
    return context
