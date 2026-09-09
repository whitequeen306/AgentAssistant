"""Stable data contracts shared by subagent producers and consumers."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


class SubagentRole(StrEnum):
    """Supported subagent responsibilities."""

    EXPLORER = "explorer"
    RESEARCHER = "researcher"
    OPERATOR = "operator"
    ORGANIZER = "organizer"


class SubagentStatus(StrEnum):
    """Lifecycle states for a subagent task."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATUSES = frozenset(
    {
        SubagentStatus.COMPLETED,
        SubagentStatus.FAILED,
        SubagentStatus.CANCELLED,
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _role(value: SubagentRole | str) -> SubagentRole:
    try:
        return SubagentRole(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("role must be a supported subagent role") from exc


def _positive_int(value: int, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be an integer of at least 1")
    return value


def _utc_timestamp(value: str | None, field_name: str, *, optional: bool) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field_name} must be a UTC ISO-8601 timestamp")
    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UTC ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must have a UTC offset of zero")
    return text


def _require_dict(value: Any, field_name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a dictionary")


def _json_safe(value: Any, seen: set[int] | None = None) -> Any:
    if isinstance(value, Enum):
        return _json_safe(value.value, seen)
    if isinstance(value, float) and not math.isfinite(value):
        return "<non-finite float>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"

    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "<recursive reference>"

    if is_dataclass(value) and not isinstance(value, type):
        seen.add(identity)
        try:
            return {
                item.name: _json_safe(getattr(value, item.name), seen) for item in fields(value)
            }
        finally:
            seen.remove(identity)
    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            converted: dict[str, Any] = {}
            for key, item in value.items():
                base_key = _json_safe_key(key, seen)
                output_key = base_key
                suffix = 2
                while output_key in converted:
                    output_key = f"{base_key}#{suffix}"
                    suffix += 1
                converted[output_key] = _json_safe(item, seen)
            return converted
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        seen.add(identity)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, set):
        seen.add(identity)
        try:
            converted = [_json_safe(item, seen) for item in value]
            return sorted(converted, key=_json_sort_key)
        finally:
            seen.remove(identity)
    return "<unsupported object>"


def _json_safe_key(value: Any, seen: set[int]) -> str:
    if isinstance(value, Enum):
        return f"<enum:{_json_key_component(_json_safe(value.value, seen))}>"
    if isinstance(value, str):
        return value
    if value is None:
        return "<none>"
    if isinstance(value, bool):
        return f"<bool:{str(value).lower()}>"
    if isinstance(value, int):
        return f"<int:{value}>"
    if isinstance(value, float):
        return f"<float:{_json_key_component(_json_safe(value, seen))}>"
    if isinstance(value, Path):
        return f"<path:{value}>"
    return "<object>"


def _json_key_component(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    return _json_sort_key(value)


def _json_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class SubagentSpec:
    """Definition and lifecycle metadata for one delegated task."""

    task_id: str
    conversation_id: str
    parent_turn_id: str
    role: SubagentRole
    title: str
    goal: str
    context: dict[str, Any] = field(default_factory=dict)
    status: SubagentStatus = SubagentStatus.CREATED
    attempt: int = 1
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    updated_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    previous_status: SubagentStatus | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        self.task_id = _required_text(self.task_id, "task_id")
        self.conversation_id = _required_text(self.conversation_id, "conversation_id")
        self.parent_turn_id = _required_text(self.parent_turn_id, "parent_turn_id")
        self.role = _role(self.role)
        self.title = _required_text(self.title, "title")
        self.goal = _required_text(self.goal, "goal")
        _require_dict(self.context, "context")
        self.status = SubagentStatus(self.status)
        if self.previous_status is not None:
            self.previous_status = SubagentStatus(self.previous_status)
        self.attempt = _positive_int(self.attempt, "attempt")
        self.created_at = _utc_timestamp(self.created_at, "created_at", optional=False)
        self.started_at = _utc_timestamp(self.started_at, "started_at", optional=True)
        self.updated_at = _utc_timestamp(self.updated_at, "updated_at", optional=False)
        self.finished_at = _utc_timestamp(self.finished_at, "finished_at", optional=True)

    @classmethod
    def create(
        cls,
        *,
        conversation_id: str,
        parent_turn_id: str,
        role: SubagentRole | str,
        title: str,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> SubagentSpec:
        """Create a validated task in its initial lifecycle state."""

        now = _utc_now()
        return cls(
            task_id=uuid4().hex[:12],
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
            role=_role(role),
            title=title,
            goal=goal,
            context={} if context is None else context,
            status=SubagentStatus.CREATED,
            attempt=1,
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe task specification."""

        return _json_safe(
            {
                "task_id": self.task_id,
                "conversation_id": self.conversation_id,
                "parent_turn_id": self.parent_turn_id,
                "role": self.role,
                "title": self.title,
                "goal": self.goal,
                "context": self.context,
                "status": self.status,
                "attempt": self.attempt,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "finished_at": self.finished_at,
                "previous_status": self.previous_status,
                "error_category": self.error_category,
            }
        )


@dataclass(slots=True)
class SubagentEvent:
    """Ordered event emitted by a subagent task."""

    event_id: str
    conversation_id: str
    parent_turn_id: str
    task_id: str
    sequence: int
    role: SubagentRole
    type: str
    payload: dict[str, Any]
    timestamp: str

    def __post_init__(self) -> None:
        self.event_id = _required_text(self.event_id, "event_id")
        self.conversation_id = _required_text(self.conversation_id, "conversation_id")
        self.parent_turn_id = _required_text(self.parent_turn_id, "parent_turn_id")
        self.task_id = _required_text(self.task_id, "task_id")
        self.type = _required_text(self.type, "type")
        self.role = _role(self.role)
        self.sequence = _positive_int(self.sequence, "sequence")
        _require_dict(self.payload, "payload")
        self.timestamp = _utc_timestamp(self.timestamp, "timestamp", optional=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe event representation."""

        return _json_safe(
            {
                "event_id": self.event_id,
                "conversation_id": self.conversation_id,
                "parent_turn_id": self.parent_turn_id,
                "task_id": self.task_id,
                "sequence": self.sequence,
                "role": self.role,
                "type": self.type,
                "payload": self.payload,
                "timestamp": self.timestamp,
            }
        )


@dataclass(slots=True)
class SubagentResult:
    """Uniform terminal result returned by every subagent role."""

    task_id: str
    role: SubagentRole
    status: SubagentStatus
    attempt: int
    summary: str = ""
    output: Any = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    partial_result: Any = None
    next_action: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    error_category: str | None = None
    error: str | None = None
    l0_raw: Any = None
    l1_trace: Any = None

    def __post_init__(self) -> None:
        self.task_id = _required_text(self.task_id, "task_id")
        self.role = _role(self.role)
        self.status = SubagentStatus(self.status)
        if self.status not in TERMINAL_STATUSES:
            raise ValueError("result status must be terminal")
        self.attempt = _positive_int(self.attempt, "attempt")
        self.summary = _required_text(self.summary, "summary")
        _require_dict(self.artifacts, "artifacts")
        if not isinstance(self.evidence, list):
            raise ValueError("evidence must be a list")
        _require_dict(self.stats, "stats")
        if self.status is SubagentStatus.FAILED:
            self.error_category = _required_text(self.error_category, "error_category")
            self.error = _required_text(self.error, "error")
        elif self.status is SubagentStatus.CANCELLED:
            category = self.error_category or "user_cancelled"
            self.error_category = _required_text(category, "error_category")
        elif self.error is not None or self.error_category is not None:
            raise ValueError("completed results cannot contain error details")

    @classmethod
    def completed(
        cls,
        *,
        task_id: str,
        role: SubagentRole | str,
        attempt: int,
        summary: str,
        output: Any,
        artifacts: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        next_action: str | None = None,
        stats: dict[str, Any] | None = None,
        l0_raw: Any = None,
        l1_trace: Any = None,
    ) -> SubagentResult:
        """Build a successful terminal result."""

        return cls(
            task_id=task_id,
            role=_role(role),
            status=SubagentStatus.COMPLETED,
            attempt=attempt,
            summary=summary,
            output=output,
            artifacts={} if artifacts is None else artifacts,
            evidence=[] if evidence is None else evidence,
            next_action=next_action,
            stats={} if stats is None else stats,
            l0_raw=l0_raw,
            l1_trace=l1_trace,
        )

    @classmethod
    def failure(
        cls,
        *,
        task_id: str,
        role: SubagentRole | str,
        attempt: int,
        summary: str,
        error_category: str,
        error: str,
        partial_result: Any = None,
        next_action: str | None = None,
        stats: dict[str, Any] | None = None,
        l0_raw: Any = None,
        l1_trace: Any = None,
    ) -> SubagentResult:
        """Build a failed terminal result with a caller-safe error."""

        return cls(
            task_id=task_id,
            role=_role(role),
            status=SubagentStatus.FAILED,
            attempt=attempt,
            summary=summary,
            partial_result=partial_result,
            next_action=next_action,
            stats={} if stats is None else stats,
            error_category=error_category,
            error=error,
            l0_raw=l0_raw,
            l1_trace=l1_trace,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        task_id: str,
        role: SubagentRole | str,
        attempt: int,
        summary: str,
        partial_result: Any = None,
        next_action: str | None = None,
        stats: dict[str, Any] | None = None,
        error_category: str = "user_cancelled",
        l0_raw: Any = None,
        l1_trace: Any = None,
    ) -> SubagentResult:
        """Build a cancelled terminal result, optionally retaining partial work."""

        return cls(
            task_id=task_id,
            role=_role(role),
            status=SubagentStatus.CANCELLED,
            attempt=attempt,
            summary=summary,
            partial_result=partial_result,
            next_action=next_action,
            stats={} if stats is None else stats,
            error_category=error_category,
            l0_raw=l0_raw,
            l1_trace=l1_trace,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return every field in the stable, JSON-safe result schema."""

        return _json_safe(
            {
                "task_id": self.task_id,
                "role": self.role,
                "status": self.status,
                "attempt": self.attempt,
                "summary": self.summary,
                "output": self.output,
                "artifacts": self.artifacts,
                "evidence": self.evidence,
                "partial_result": self.partial_result,
                "next_action": self.next_action,
                "stats": self.stats,
                "error_category": self.error_category,
                "error": self.error,
                "l0_raw": self.l0_raw,
                "l1_trace": self.l1_trace,
            }
        )
