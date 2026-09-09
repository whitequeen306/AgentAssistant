import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

from agent_assistant.subagents.models import (
    TERMINAL_STATUSES,
    SubagentEvent,
    SubagentResult,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)


class PayloadKind(Enum):
    FILE = "file"


@dataclass
class PayloadRecord:
    path: Path
    role: SubagentRole


class UnknownPayload:
    def __repr__(self) -> str:
        return "<UnknownPayload secret=do-not-leak at 0x123456>"


def make_spec(**overrides: object) -> SubagentSpec:
    values = {
        "task_id": "task-1",
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "role": SubagentRole.EXPLORER,
        "title": "标题",
        "goal": "目标",
        "created_at": "2026-08-12T12:00:00+00:00",
        "updated_at": "2026-08-12T12:00:00+00:00",
    }
    values.update(overrides)
    return SubagentSpec(**values)


def make_event(**overrides: object) -> SubagentEvent:
    values = {
        "event_id": "event-1",
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "task_id": "task-1",
        "sequence": 1,
        "role": SubagentRole.EXPLORER,
        "type": "progress",
        "payload": {},
        "timestamp": "2026-08-12T12:00:00+00:00",
    }
    values.update(overrides)
    return SubagentEvent(**values)


def make_result(**overrides: object) -> SubagentResult:
    values = {
        "task_id": "task-1",
        "role": SubagentRole.EXPLORER,
        "status": SubagentStatus.COMPLETED,
        "attempt": 1,
        "summary": "完成",
    }
    values.update(overrides)
    return SubagentResult(**values)


def test_spec_create_normalizes_role_and_initializes_contract() -> None:
    spec = SubagentSpec.create(
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role="explorer",
        title="查找考研资料",
        goal="在 Documents 中查找考研资料",
    )

    assert spec.role is SubagentRole.EXPLORER
    assert spec.status is SubagentStatus.CREATED
    assert len(spec.task_id) == 12
    assert spec.task_id
    assert spec.attempt == 1
    assert spec.created_at.endswith("+00:00")
    assert spec.updated_at == spec.created_at


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", "   "),
        ("goal", ""),
        ("goal", "\t"),
    ],
)
def test_spec_create_rejects_empty_text(field: str, value: str) -> None:
    values = {
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "role": "explorer",
        "title": "有效标题",
        "goal": "有效目标",
    }
    values[field] = value

    with pytest.raises(ValueError):
        SubagentSpec.create(**values)


def test_spec_create_strips_title_and_goal() -> None:
    spec = SubagentSpec.create(
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=SubagentRole.RESEARCHER,
        title="  标题  ",
        goal="\n目标\t",
    )

    assert spec.title == "标题"
    assert spec.goal == "目标"


def test_spec_create_rejects_invalid_role() -> None:
    with pytest.raises(ValueError):
        SubagentSpec.create(
            conversation_id="conv-1",
            parent_turn_id="turn-1",
            role="administrator",
            title="标题",
            goal="目标",
        )


def test_completed_result_serializes_all_unified_fields() -> None:
    result = SubagentResult.completed(
        task_id="task-1",
        role=SubagentRole.EXPLORER,
        attempt=1,
        summary="找到 3 个文件",
        output="三个文件均位于 Documents",
        evidence=[{"type": "file", "ref": "C:/Users/u/Documents/a.pdf"}],
    ).to_dict()

    assert result["status"] == "completed"
    assert result["attempt"] == 1
    assert result["partial_result"] is None
    assert "error_category" in result
    assert set(result) == {
        "task_id",
        "role",
        "status",
        "attempt",
        "summary",
        "output",
        "artifacts",
        "evidence",
        "partial_result",
        "next_action",
        "stats",
        "error_category",
        "error",
        "l0_raw",
        "l1_trace",
    }


def test_failure_and_cancelled_are_explicit_terminal_constructors() -> None:
    failure = SubagentResult.failure(
        task_id="task-1",
        role="operator",
        attempt=2,
        summary="操作失败",
        error_category="permission_denied",
        error="无法执行该操作",
    )
    cancelled = SubagentResult.cancelled(
        task_id="task-2",
        role=SubagentRole.ORGANIZER,
        attempt=1,
        summary="用户取消",
        partial_result="已完成分类",
    )

    assert failure.status is SubagentStatus.FAILED
    assert failure.role is SubagentRole.OPERATOR
    assert cancelled.status is SubagentStatus.CANCELLED
    assert cancelled.partial_result == "已完成分类"
    assert TERMINAL_STATUSES == {
        SubagentStatus.COMPLETED,
        SubagentStatus.FAILED,
        SubagentStatus.CANCELLED,
    }
    assert isinstance(TERMINAL_STATUSES, frozenset)


def test_event_serialization_preserves_sequence_and_is_json_safe() -> None:
    event = SubagentEvent(
        event_id="event-1",
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        task_id="task-1",
        sequence=3,
        role=SubagentRole.EXPLORER,
        type="progress",
        payload={"count": 2},
        timestamp="2026-08-12T12:00:00+00:00",
    )

    serialized = event.to_dict()

    assert serialized["sequence"] == 3
    assert json.loads(json.dumps(serialized))["sequence"] == 3


def test_event_serializes_nested_non_json_payload_safely_and_deterministically() -> None:
    event = SubagentEvent(
        event_id="event-1",
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        task_id="task-1",
        sequence=1,
        role=SubagentRole.EXPLORER,
        type="progress",
        payload={
            "path": Path("C:/Users/u/Documents/a.pdf"),
            "enum": PayloadKind.FILE,
            "record": PayloadRecord(Path("C:/tmp/report.txt"), SubagentRole.RESEARCHER),
            "set": {"beta", "alpha"},
            "tuple": (1, "two"),
            "bytes": b"\xffsecret",
            "unknown": UnknownPayload(),
        },
        timestamp="2026-08-12T12:00:00+00:00",
    )

    payload = event.to_dict()["payload"]

    assert payload == {
        "path": str(Path("C:/Users/u/Documents/a.pdf")),
        "enum": "file",
        "record": {
            "path": str(Path("C:/tmp/report.txt")),
            "role": "researcher",
        },
        "set": ["alpha", "beta"],
        "tuple": [1, "two"],
        "bytes": "<bytes:7>",
        "unknown": "<unsupported object>",
    }
    json.dumps(event.to_dict())


def test_result_and_spec_nested_data_are_json_safe() -> None:
    nested = {
        "path": Path("C:/tmp/input.txt"),
        "record": PayloadRecord(Path("C:/tmp/output.txt"), SubagentRole.OPERATOR),
        "values": {3, 1, 2},
        "raw": b"data",
        "unknown": UnknownPayload(),
    }
    result = SubagentResult.completed(
        task_id="task-1",
        role=SubagentRole.EXPLORER,
        attempt=1,
        summary="完成",
        output=nested,
    )
    spec = SubagentSpec.create(
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=SubagentRole.EXPLORER,
        title="标题",
        goal="目标",
        context=nested,
    )

    serialized_result = result.to_dict()
    serialized_spec = spec.to_dict()

    assert serialized_result["output"]["values"] == [1, 2, 3]
    assert serialized_spec["context"]["raw"] == "<bytes:4>"
    assert serialized_spec["context"]["unknown"] == "<unsupported object>"
    json.dumps(serialized_result)
    json.dumps(serialized_spec)


def test_non_finite_floats_are_strict_json_safe_without_dropping_fields() -> None:
    values = {"nan": float("nan"), "positive": float("inf"), "negative": float("-inf")}
    event = make_event(payload=values)
    result = make_result(output=values)
    spec = make_spec(context=values)

    for serialized in (event.to_dict(), result.to_dict(), spec.to_dict()):
        assert json.dumps(serialized, allow_nan=False)

    assert event.to_dict()["payload"] == {
        "nan": "<non-finite float>",
        "positive": "<non-finite float>",
        "negative": "<non-finite float>",
    }


def test_recursive_payload_is_replaced_with_safe_marker() -> None:
    payload: dict[str, object] = {}
    payload["self"] = payload

    serialized = make_event(payload=payload).to_dict()

    assert serialized["payload"]["self"] == "<recursive reference>"
    json.dumps(serialized, allow_nan=False)


def test_mapping_keys_are_typed_and_collisions_are_not_overwritten() -> None:
    first_unknown = UnknownPayload()
    second_unknown = UnknownPayload()
    payload = {
        "<int:1>": "string key",
        1: "integer key",
        PayloadKind.FILE: "enum key",
        SubagentRole.EXPLORER: "str enum key",
        Path("C:/tmp/a.txt"): "path key",
        first_unknown: "first unknown",
        second_unknown: "second unknown",
    }

    serialized = make_event(payload=payload).to_dict()["payload"]

    assert serialized["<int:1>"] == "string key"
    assert serialized["<int:1>#2"] == "integer key"
    assert serialized["<enum:file>"] == "enum key"
    assert serialized["<enum:explorer>"] == "str enum key"
    assert serialized[f"<path:{Path('C:/tmp/a.txt')}>"] == "path key"
    assert serialized["<object>"] == "first unknown"
    assert serialized["<object>#2"] == "second unknown"
    encoded = json.dumps(serialized, allow_nan=False)
    assert "secret=do-not-leak" not in encoded
    assert "0x123456" not in encoded


def test_result_rejects_non_terminal_status() -> None:
    with pytest.raises(ValueError):
        make_result(status=SubagentStatus.RUNNING)


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": ""},
        {"summary": " "},
        {"attempt": True},
        {"attempt": 0},
        {"artifacts": []},
        {"evidence": {}},
        {"stats": []},
    ],
)
def test_result_rejects_invalid_identity_attempt_and_containers(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        make_result(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"error_category": None, "error": "安全错误"},
        {"error_category": "category", "error": ""},
    ],
)
def test_failed_result_requires_error_details(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        make_result(status=SubagentStatus.FAILED, **overrides)


def test_cancelled_result_defaults_error_category() -> None:
    result = make_result(status=SubagentStatus.CANCELLED)

    assert result.error_category == "user_cancelled"


def test_completed_result_rejects_error() -> None:
    with pytest.raises(ValueError):
        make_result(error="unexpected")


def test_completed_result_rejects_error_category() -> None:
    with pytest.raises(ValueError):
        make_result(error_category="unexpected_category")


@pytest.mark.parametrize(
    ("factory", "overrides"),
    [
        (make_spec, {"attempt": True}),
        (make_spec, {"attempt": 1.5}),
        (make_spec, {"context": []}),
        (make_event, {"sequence": True}),
        (make_event, {"sequence": 1.5}),
        (make_event, {"payload": []}),
    ],
)
def test_specs_and_events_reject_invalid_numbers_and_containers(
    factory: object,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        factory(**overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("created_at", ""),
        ("created_at", "not-a-time"),
        ("created_at", "2026-08-12T12:00:00"),
        ("updated_at", "2026-08-12T20:00:00+08:00"),
        ("started_at", "2026-08-12T12:00:00+01:00"),
        ("finished_at", " "),
    ],
)
def test_spec_rejects_invalid_or_non_utc_times(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        make_spec(**{field: value})


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "not-a-time",
        "2026-08-12T12:00:00",
        "2026-08-12T20:00:00+08:00",
    ],
)
def test_event_rejects_invalid_or_non_utc_timestamp(timestamp: str) -> None:
    with pytest.raises(ValueError):
        make_event(timestamp=timestamp)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 0},
        {"event_id": ""},
        {"conversation_id": " "},
        {"parent_turn_id": ""},
        {"task_id": "\t"},
        {"type": ""},
    ],
)
def test_event_rejects_invalid_identity_and_sequence(overrides: dict[str, object]) -> None:
    values = {
        "event_id": "event-1",
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "task_id": "task-1",
        "sequence": 1,
        "role": SubagentRole.EXPLORER,
        "type": "progress",
        "payload": {},
        "timestamp": "2026-08-12T12:00:00+00:00",
    }
    values.update(overrides)

    with pytest.raises(ValueError):
        SubagentEvent(**values)
