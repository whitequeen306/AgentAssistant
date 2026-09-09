"""Lazy process-wide SubagentManager wiring.

``launch.py`` calls ``set_event_sink`` + ``recover_on_startup`` once at boot;
tools and the bridge reach the manager through ``get_manager()``. Runners are
registered per role as they are implemented (Researcher, Explorer, Operator,
Organizer) — dispatching an unregistered role fails cleanly at dispatch time.
"""

from __future__ import annotations

import logging
import threading

from .manager import EventSink, RunnerFn, SubagentManager
from .models import SubagentRole
from .store import SubagentStore

logger = logging.getLogger(__name__)

_guard = threading.Lock()
_manager: SubagentManager | None = None
_runners: dict[SubagentRole, RunnerFn] = {}
_pending_sink: EventSink | None = None


class RunnerNotRegisteredError(RuntimeError):
    """No runner is registered for the requested role."""


def register_runner(role: SubagentRole | str, runner: RunnerFn) -> None:
    _runners[SubagentRole(role)] = runner


def runner_for(role: SubagentRole | str) -> RunnerFn:
    try:
        return _runners[SubagentRole(role)]
    except KeyError:
        raise RunnerNotRegisteredError(
            f"no runner registered for role '{SubagentRole(role).value}'"
        ) from None


def registered_roles() -> frozenset[SubagentRole]:
    return frozenset(_runners)


def set_event_sink(sink: EventSink | None) -> None:
    """Bind the UI event sink (before or after manager creation)."""
    global _pending_sink
    with _guard:
        _pending_sink = sink
        if _manager is not None:
            _manager.set_event_sink(sink)


def _register_default_runners() -> None:
    """Register the built-in role runners (idempotent, lazy imports)."""
    if SubagentRole.RESEARCHER not in _runners:
        from .runners.researcher import researcher_runner

        _runners[SubagentRole.RESEARCHER] = researcher_runner
    if SubagentRole.EXPLORER not in _runners:
        from .runners.explorer import explorer_runner

        _runners[SubagentRole.EXPLORER] = explorer_runner
    if SubagentRole.OPERATOR not in _runners:
        from .runners.operator import operator_runner

        _runners[SubagentRole.OPERATOR] = operator_runner
    if SubagentRole.ORGANIZER not in _runners:
        from .runners.organizer import organizer_runner

        _runners[SubagentRole.ORGANIZER] = organizer_runner


def get_manager() -> SubagentManager:
    """Create (once) and return the process-wide manager."""
    global _manager
    with _guard:
        _register_default_runners()
        if _manager is None:
            from agent_assistant.config import settings

            settings.subagent_runs_dir.mkdir(parents=True, exist_ok=True)
            store = SubagentStore(settings.subagent_db_path)
            _manager = SubagentManager(
                store,
                runner_for=runner_for,
                event_sink=_pending_sink,
                archive_root=settings.subagent_runs_dir,
                max_workers=settings.subagent_max_concurrency,
            )
        return _manager


def recover_on_startup() -> int:
    """Mark persisted non-terminal tasks interrupted; returns how many."""
    try:
        recovered = get_manager().recover()
    except Exception:
        logger.exception("Subagent startup recovery failed")
        return 0
    if recovered:
        logger.info("Recovered %d interrupted subagent task(s)", len(recovered))
    return len(recovered)


def reset_for_tests() -> None:
    """Drop the singleton (tests only)."""
    global _manager, _pending_sink
    with _guard:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
        _pending_sink = None
        _runners.clear()
