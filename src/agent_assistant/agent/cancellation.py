"""Cooperative cancel for the active agent turn (main loop + tools/sub-agents)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_current: ContextVar[threading.Event | None] = ContextVar(
    "agent_cancel_event",
    default=None,
)


@contextmanager
def bind_cancel(event: threading.Event) -> Iterator[None]:
    """Bind ``event`` as the cancel signal for this turn (incl. tool threads)."""
    token = _current.set(event)
    try:
        yield
    finally:
        _current.reset(token)


def is_cancelled() -> bool:
    ev = _current.get()
    return bool(ev is not None and ev.is_set())


def request_cancel() -> bool:
    """Signal the currently bound turn to stop. Returns False if none bound."""
    ev = _current.get()
    if ev is None:
        return False
    ev.set()
    return True
