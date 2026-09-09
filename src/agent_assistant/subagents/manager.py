"""In-process subagent task manager: scheduling, control, events, recovery.

Design (spec §6):
- A four-slot ``ThreadPoolExecutor`` runs task bodies; one scheduler thread
  pulls from a FIFO queue and acquires each task's resources (desktop /
  filesystem_write) BEFORE submitting, so a task waiting on a lock never
  occupies a worker slot.
- Every state change is persisted to the ``SubagentStore`` before it is
  published to the event sink (persist-before-publish).
- Event ordering is delegated to the store's token-owned sequence allocation;
  the manager only tracks the latest published sequence per task.
- Cancellation and pause are cooperative via per-task ``threading.Event``s.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent_assistant.tools.sanitize import sanitize_error

from .archive import TaskArchive, TaskArchiveError
from .models import (
    TERMINAL_STATUSES,
    SubagentEvent,
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)
from .profiles import profile_for
from .resources import (
    DESKTOP,
    FILESYSTEM_WRITE,
    ResourceCoordinator,
    ResourceLease,
    resource_coordinator,
)
from .store import SubagentStore, SubagentStoreError

logger = logging.getLogger(__name__)

CONTROL_ACTIONS = frozenset({"cancel", "pause", "resume", "retry", "continue"})


def task_owner_id(task_id: str) -> str:
    """Resource/lock owner identity for a subagent task."""
    return f"subagent:{task_id}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class PausedOutcome:
    """Runner yielded cooperatively at a safe boundary (Operator pause)."""

    checkpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WaitingUserOutcome:
    """Runner needs a user decision (Organizer manifest approval).

    ``checkpoint`` is the authoritative on-disk state (manifest + hash);
    ``prompt`` is a small UI-safe summary published with the status event.
    """

    checkpoint: dict[str, Any] = field(default_factory=dict)
    prompt: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskControls:
    """Cooperative control signals handed to a runner."""

    task_id: str
    cancel_event: threading.Event
    pause_event: threading.Event

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def paused(self) -> bool:
        return self.pause_event.is_set()

    def should_yield(self) -> bool:
        """Runner must stop at the next safe boundary."""
        return self.cancelled() or self.paused()


EmitFn = Callable[[str, dict[str, Any]], None]
RunnerFn = Callable[
    [SubagentSpec, TaskControls, EmitFn],
    "SubagentResult | PausedOutcome | WaitingUserOutcome",
]
EventSink = Callable[[SubagentEvent], None]


class SubagentManager:
    """Schedules, controls, and persists subagent tasks."""

    def __init__(
        self,
        store: SubagentStore,
        *,
        runner_for: Callable[[SubagentRole], RunnerFn],
        event_sink: EventSink | None = None,
        archive_root: Path | None = None,
        max_workers: int = 4,
        resources: ResourceCoordinator | None = None,
    ) -> None:
        self._store = store
        self._runner_for = runner_for
        self._sink: EventSink = event_sink or (lambda event: None)
        self._archive_root = Path(archive_root) if archive_root is not None else None
        self._resources = resources if resources is not None else resource_coordinator
        self._max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="subagent"
        )
        self._condition = threading.Condition()
        self._queue: deque[str] = deque()
        self._specs: dict[str, SubagentSpec] = {}
        self._controls: dict[str, TaskControls] = {}
        self._results: dict[str, SubagentResult] = {}
        self._leases: dict[str, list[ResourceLease]] = {}
        self._active: set[str] = set()
        self._last_sequence: dict[str, int] = {}
        self._shutdown = False
        self._scheduler = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="subagent-scheduler"
        )
        self._scheduler.start()

    # ─── Wiring ────────────────────────────────────────────────────────────

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._sink = sink or (lambda event: None)

    # ─── Dispatch ──────────────────────────────────────────────────────────

    def dispatch(self, specs: list[SubagentSpec]) -> list[str]:
        """Persist tasks, then queue them. Returns task IDs in input order."""
        task_ids: list[str] = []
        for spec in specs:
            # Fail fast on roles without a registered runner.
            self._runner_for(spec.role)
            queued = replace(spec, status=SubagentStatus.QUEUED, updated_at=_utc_now())
            self._store.create_task(queued)  # persist BEFORE queueing/publishing
            with self._condition:
                self._specs[queued.task_id] = queued
                self._controls[queued.task_id] = TaskControls(
                    queued.task_id, threading.Event(), threading.Event()
                )
                self._queue.append(queued.task_id)
                self._condition.notify_all()
            self.emit(
                queued.task_id,
                "status",
                {
                    "status": SubagentStatus.QUEUED.value,
                    "title": queued.title,
                    "goal": queued.goal,
                    "role": queued.role.value,
                    "attempt": queued.attempt,
                },
            )
            task_ids.append(queued.task_id)
        return task_ids

    # ─── Scheduler ─────────────────────────────────────────────────────────

    def _scheduler_loop(self) -> None:
        while True:
            with self._condition:
                if self._shutdown:
                    return
                picked = self._try_pick_locked()
                if picked is None:
                    # Woken by dispatch/finish/release; timeout re-checks
                    # externally released resources (main-agent ephemeral use).
                    self._condition.wait(timeout=0.1)
                    continue
                task_id, leases = picked
                self._active.add(task_id)
                self._leases[task_id] = leases
            try:
                self._executor.submit(self._run_task, task_id)
            except RuntimeError:  # executor shut down during teardown
                with self._condition:
                    self._active.discard(task_id)
                    for lease in self._leases.pop(task_id, []):
                        lease.release()
                return

    def _try_pick_locked(self) -> tuple[str, list[ResourceLease]] | None:
        """Pick the first queued task whose resources are all acquirable."""
        if len(self._active) >= self._max_workers:
            return None
        for task_id in list(self._queue):
            spec = self._specs.get(task_id)
            if spec is None:
                self._queue.remove(task_id)
                continue
            needed = self._needed_resources(spec)
            leases: list[ResourceLease] = []
            blocked = False
            for resource in needed:
                lease = self._resources.try_acquire(resource, task_owner_id(task_id))
                if lease is None:
                    blocked = True
                    break
                leases.append(lease)
            if blocked:
                for lease in leases:
                    lease.release()
                continue  # try the next queued task; do not starve others
            self._queue.remove(task_id)
            return task_id, leases
        return None

    @staticmethod
    def _needed_resources(spec: SubagentSpec) -> list[str]:
        profile = profile_for(spec.role)
        needed: list[str] = []
        if profile.needs_desktop:
            needed.append(DESKTOP)
        if profile.needs_filesystem_write:
            needed.append(FILESYSTEM_WRITE)
        return needed

    # ─── Task body ─────────────────────────────────────────────────────────

    def _run_task(self, task_id: str) -> None:
        with self._condition:
            spec = self._specs.get(task_id)
            controls = self._controls.get(task_id)
        if spec is None or controls is None:
            return
        if controls.cancelled():
            self._finish(task_id, self._synthesize_cancelled(spec))
            return

        running = replace(
            spec,
            status=SubagentStatus.RUNNING,
            started_at=spec.started_at or _utc_now(),
            updated_at=_utc_now(),
        )
        try:
            self._store.update_task(running)
        except SubagentStoreError:
            logger.exception("Failed to persist running state for %s", task_id[:8])
        with self._condition:
            self._specs[task_id] = running
        self.emit(
            task_id,
            "status",
            {"status": SubagentStatus.RUNNING.value, "attempt": running.attempt},
        )

        outcome: SubagentResult | PausedOutcome | WaitingUserOutcome
        try:
            runner = self._runner_for(running.role)
            outcome = runner(
                running,
                controls,
                lambda event_type, payload: self.emit(task_id, event_type, payload),
            )
        except Exception as exc:
            if controls.cancelled():
                outcome = self._synthesize_cancelled(running)
            else:
                safe = sanitize_error(str(exc), context="subagent_runner")
                outcome = SubagentResult.failure(
                    task_id=task_id,
                    role=running.role,
                    attempt=running.attempt,
                    summary="子任务执行中断，进度已保留",
                    error_category=safe.category,
                    error=safe.safe_message,
                    partial_result=self._read_checkpoint(task_id),
                )

        if isinstance(outcome, PausedOutcome):
            self._pause_task(task_id, outcome)
            return
        if isinstance(outcome, WaitingUserOutcome):
            self._wait_user_task(task_id, outcome)
            return
        self._finish(task_id, outcome)

    def _pause_task(self, task_id: str, outcome: PausedOutcome) -> None:
        self._write_checkpoint(task_id, outcome.checkpoint)
        with self._condition:
            spec = self._specs.get(task_id)
        if spec is not None:
            paused = replace(
                spec,
                status=SubagentStatus.PAUSED,
                previous_status=spec.status,
                updated_at=_utc_now(),
            )
            try:
                self._store.update_task(paused)
            except SubagentStoreError:
                logger.exception("Failed to persist paused state for %s", task_id[:8])
            with self._condition:
                self._specs[task_id] = paused
        self.emit(task_id, "status", {"status": SubagentStatus.PAUSED.value})
        with self._condition:
            self._active.discard(task_id)
            for lease in self._leases.pop(task_id, []):
                lease.release()
            controls = self._controls.get(task_id)
            if controls is not None:
                controls.pause_event.clear()
            self._condition.notify_all()

    def _wait_user_task(self, task_id: str, outcome: WaitingUserOutcome) -> None:
        """Park the task on a user decision; resources are released meanwhile."""
        self._write_checkpoint(task_id, outcome.checkpoint)
        with self._condition:
            spec = self._specs.get(task_id)
        if spec is not None:
            waiting = replace(
                spec,
                status=SubagentStatus.WAITING_USER,
                previous_status=spec.status,
                updated_at=_utc_now(),
            )
            try:
                self._store.update_task(waiting)
            except SubagentStoreError:
                logger.exception("Failed to persist waiting state for %s", task_id[:8])
            with self._condition:
                self._specs[task_id] = waiting
        self.emit(
            task_id,
            "status",
            {"status": SubagentStatus.WAITING_USER.value, **(outcome.prompt or {})},
        )
        with self._condition:
            self._active.discard(task_id)
            for lease in self._leases.pop(task_id, []):
                lease.release()
            self._condition.notify_all()

    def respond_organizer_plan(
        self,
        task_id: str,
        manifest_hash_value: str,
        approved: bool,
    ) -> dict[str, Any]:
        """User decision on an Organizer manifest (single-use, hash-bound)."""
        import hmac as hmac_module

        from .manifest import approval_expired

        spec = self.get_task(task_id)
        if spec is None or spec.role is not SubagentRole.ORGANIZER:
            return {"ok": False, "error": "该任务不是整理任务", "error_category": "wrong_role"}
        if spec.status is not SubagentStatus.WAITING_USER:
            return {"ok": False, "error": "该任务当前不在等待审批状态"}
        archive = self._archive(task_id)
        checkpoint = self._read_checkpoint(task_id)
        if archive is None or not isinstance(checkpoint, dict):
            return {"ok": False, "error": "找不到待审批的整理计划"}
        if checkpoint.get("phase") != "awaiting_approval":
            return {"ok": False, "error": "该计划已处理过"}
        manifest = checkpoint.get("manifest")
        stored_hash = str(checkpoint.get("manifest_hash") or "")
        supplied = str(manifest_hash_value or "")
        if not manifest or not stored_hash:
            return {"ok": False, "error": "整理计划数据缺失"}
        if not hmac_module.compare_digest(stored_hash, supplied):
            return {"ok": False, "error": "计划内容已变化，请重新查看后再确认"}
        if approval_expired(manifest):
            checkpoint["phase"] = "expired"
            self._write_checkpoint(task_id, checkpoint)
            self._finish(
                task_id,
                SubagentResult.cancelled(
                    task_id=task_id,
                    role=spec.role,
                    attempt=spec.attempt,
                    summary="整理计划审批超时，已取消（未移动任何文件）",
                    error_category="approval_expired",
                    partial_result={"manifest_hash": stored_hash},
                ),
            )
            return {"ok": False, "error": "审批已超时，任务已取消"}

        if not approved:
            checkpoint["phase"] = "denied"
            self._write_checkpoint(task_id, checkpoint)
            self._finish(
                task_id,
                SubagentResult.cancelled(
                    task_id=task_id,
                    role=spec.role,
                    attempt=spec.attempt,
                    summary="用户拒绝了整理计划，未移动任何文件",
                    error_category="user_denied",
                    partial_result={"manifest_hash": stored_hash},
                ),
            )
            return {"ok": True, "approved": False}

        # Single-use: flip the phase BEFORE requeueing the apply run.
        checkpoint["phase"] = "applying"
        self._write_checkpoint(task_id, checkpoint)
        context = dict(spec.context)
        context["organizer_phase"] = "apply"
        context["manifest_hash"] = stored_hash
        self._requeue(spec, attempt=spec.attempt, context=context)
        return {"ok": True, "approved": True}

    def _finish(self, task_id: str, result: SubagentResult) -> None:
        with self._condition:
            spec = self._specs.get(task_id)
        if spec is not None:
            finished = replace(
                spec,
                status=result.status,
                finished_at=_utc_now(),
                updated_at=_utc_now(),
                error_category=result.error_category,
            )
            try:
                self._store.update_task(finished)
            except SubagentStoreError:
                logger.exception("Failed to persist terminal state for %s", task_id[:8])
            with self._condition:
                self._specs[task_id] = finished
        # Persist the result as an event BEFORE making it awaitable.
        self.emit(task_id, "result", {"result": result.to_dict()})
        with self._condition:
            self._results[task_id] = result
            self._active.discard(task_id)
            for lease in self._leases.pop(task_id, []):
                lease.release()
            self._condition.notify_all()

    def _synthesize_cancelled(self, spec: SubagentSpec) -> SubagentResult:
        checkpoint = self._read_checkpoint(spec.task_id)
        return SubagentResult.cancelled(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="任务已取消，进度已保留",
            partial_result=checkpoint if checkpoint is not None else {"progress": "none"},
        )

    # ─── Await ─────────────────────────────────────────────────────────────

    def await_tasks(
        self,
        task_ids: list[str],
        timeout: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[SubagentResult]:
        """Block until every task has a terminal result, the parent turn is
        cancelled, or the timeout elapses. Returns available results in
        ``task_ids`` order (missing ones are simply absent)."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                missing = [t for t in task_ids if t not in self._results]
                if not missing:
                    break
                if cancel_check is not None and cancel_check():
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                # Result arrival wakes the condition immediately; the 0.2s
                # slice exists only to observe cancel_check while waiting.
                if cancel_check is not None:
                    slice_ = 0.2 if remaining is None else min(0.2, remaining)
                else:
                    slice_ = remaining
                self._condition.wait(timeout=slice_)
            return [self._results[t] for t in task_ids if t in self._results]

    # ─── Control ───────────────────────────────────────────────────────────

    def control(
        self,
        task_id: str,
        action: str,
        instruction: str | None = None,
    ) -> SubagentSpec:
        """Apply a lifecycle action; returns the task's updated spec."""
        if action not in CONTROL_ACTIONS:
            raise ValueError("unsupported control action")
        spec = self.get_task(task_id)
        if spec is None:
            raise KeyError("unknown task")

        if action == "cancel":
            return self._control_cancel(spec)
        if action == "pause":
            return self._control_pause(spec)
        if action == "resume":
            return self._control_resume(spec)
        if action == "retry":
            return self._control_retry(spec)
        # continue
        if not (instruction or "").strip():
            raise ValueError("continue requires a non-empty instruction")
        return self._control_continue(spec, instruction=str(instruction).strip())

    def _control_cancel(self, spec: SubagentSpec) -> SubagentSpec:
        task_id = spec.task_id
        with self._condition:
            controls = self._controls.get(task_id)
            queued = task_id in self._queue
            active = task_id in self._active
            if queued:
                self._queue.remove(task_id)
            if controls is not None:
                controls.cancel_event.set()
            self._condition.notify_all()
        if active:
            # Runner observes cancel_event and returns a cancelled result.
            return self.get_task(task_id) or spec
        if spec.status in TERMINAL_STATUSES:
            return spec
        # queued / paused / waiting_user / interrupted: synthesize immediately.
        self._finish(task_id, self._synthesize_cancelled(spec))
        return self.get_task(task_id) or spec

    def _control_pause(self, spec: SubagentSpec) -> SubagentSpec:
        if spec.status is not SubagentStatus.RUNNING:
            raise ValueError("only running tasks can be paused")
        with self._condition:
            controls = self._controls.get(spec.task_id)
            if controls is None:
                raise KeyError("unknown task")
            controls.pause_event.set()
        return spec

    def _control_resume(self, spec: SubagentSpec) -> SubagentSpec:
        if spec.status is not SubagentStatus.PAUSED:
            raise ValueError("only paused tasks can be resumed")
        return self._requeue(spec, attempt=spec.attempt, context=spec.context)

    def _control_retry(self, spec: SubagentSpec) -> SubagentSpec:
        if spec.status not in {
            SubagentStatus.FAILED,
            SubagentStatus.CANCELLED,
            SubagentStatus.INTERRUPTED,
        }:
            raise ValueError("only failed, cancelled or interrupted tasks can be retried")
        return self._requeue(spec, attempt=spec.attempt + 1, context=spec.context)

    def _control_continue(self, spec: SubagentSpec, *, instruction: str) -> SubagentSpec:
        if spec.status not in {SubagentStatus.COMPLETED, SubagentStatus.FAILED}:
            raise ValueError("only completed or failed tasks accept continue")
        context = dict(spec.context)
        context["continue_instruction"] = instruction
        return self._requeue(spec, attempt=spec.attempt + 1, context=context)

    def _requeue(
        self,
        spec: SubagentSpec,
        *,
        attempt: int,
        context: dict[str, Any],
    ) -> SubagentSpec:
        requeued = replace(
            spec,
            status=SubagentStatus.QUEUED,
            previous_status=spec.status,
            attempt=attempt,
            context=context,
            finished_at=None,
            error_category=None,
            updated_at=_utc_now(),
        )
        self._store.update_task(requeued)  # persist before queueing
        with self._condition:
            self._specs[spec.task_id] = requeued
            self._results.pop(spec.task_id, None)
            self._controls[spec.task_id] = TaskControls(
                spec.task_id, threading.Event(), threading.Event()
            )
            if spec.task_id not in self._queue:
                self._queue.append(spec.task_id)
            self._condition.notify_all()
        self.emit(
            spec.task_id,
            "status",
            {"status": SubagentStatus.QUEUED.value, "attempt": attempt},
        )
        return requeued

    # ─── Events ────────────────────────────────────────────────────────────

    def emit(self, task_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Persist an ordered event, then publish it to the sink."""
        with self._condition:
            spec = self._specs.get(task_id)
        if spec is None:
            spec = self.get_task(task_id)
            if spec is None:
                return
        try:
            reservation = self._store.next_sequence(task_id)
            event = SubagentEvent(
                event_id=uuid.uuid4().hex,
                conversation_id=spec.conversation_id,
                parent_turn_id=spec.parent_turn_id,
                task_id=task_id,
                sequence=int(reservation),
                role=spec.role,
                type=event_type,
                payload=payload or {},
                timestamp=_utc_now(),
            )
            self._store.append_event(event, reservation=reservation)
        except (SubagentStoreError, ValueError):
            logger.warning("Dropped unpersistable subagent event for %s", task_id[:8])
            return
        with self._condition:
            if event.sequence > self._last_sequence.get(task_id, 0):
                self._last_sequence[task_id] = event.sequence
        try:
            self._sink(event)
        except Exception:
            logger.exception("Subagent event sink raised")

    def last_sequence(self, task_id: str) -> int:
        with self._condition:
            cached = self._last_sequence.get(task_id)
        if cached is not None:
            return cached
        events = self._store.list_events(task_id)
        return events[-1].sequence if events else 0

    # ─── Introspection ─────────────────────────────────────────────────────

    def get_task(self, task_id: str) -> SubagentSpec | None:
        with self._condition:
            cached = self._specs.get(task_id)
        if cached is not None:
            return cached
        try:
            return self._store.get_task(task_id)
        except SubagentStoreError:
            return None

    def list_tasks(self, conversation_id: str | None = None) -> list[SubagentSpec]:
        try:
            return self._store.list_tasks(conversation_id=conversation_id)
        except SubagentStoreError:
            return []

    def result_for(self, task_id: str) -> SubagentResult | None:
        with self._condition:
            return self._results.get(task_id)

    def list_events(self, task_id: str, after_sequence: int = 0) -> list[SubagentEvent]:
        try:
            return self._store.list_events(task_id, after_sequence=after_sequence)
        except SubagentStoreError:
            return []

    # ─── Recovery / teardown ───────────────────────────────────────────────

    def recover(self) -> list[SubagentSpec]:
        """Mark every persisted non-terminal task interrupted (startup)."""
        changed = self._store.mark_nonterminal_interrupted()
        for spec in changed:
            with self._condition:
                self._specs[spec.task_id] = spec
            self.emit(
                spec.task_id,
                "status",
                {
                    "status": SubagentStatus.INTERRUPTED.value,
                    "previous_status": (
                        spec.previous_status.value if spec.previous_status else None
                    ),
                },
            )
        return changed

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            for controls in self._controls.values():
                controls.cancel_event.set()
            self._condition.notify_all()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ─── Checkpoints ───────────────────────────────────────────────────────

    def _archive(self, task_id: str) -> TaskArchive | None:
        if self._archive_root is None:
            return None
        try:
            return TaskArchive(self._archive_root, task_id)
        except (TaskArchiveError, ValueError):
            return None

    def _write_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        archive = self._archive(task_id)
        if archive is None:
            return
        try:
            archive.write_checkpoint(checkpoint)
        except TaskArchiveError:
            logger.warning("Failed to write checkpoint for %s", task_id[:8])

    def _read_checkpoint(self, task_id: str) -> Any:
        archive = self._archive(task_id)
        if archive is None:
            return None
        try:
            return archive.read_checkpoint()
        except TaskArchiveError:
            return None
