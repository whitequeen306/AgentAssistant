from __future__ import annotations

import pickle
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_assistant.subagents.models import (
    SubagentEvent,
    SubagentRole,
    SubagentSpec,
    SubagentStatus,
)
from agent_assistant.subagents.store import (
    SequenceReservation,
    SubagentStore,
    SubagentStoreError,
)

NOW = "2026-08-12T12:00:00+00:00"


def make_spec(task_id: str = "task-1", **overrides: object) -> SubagentSpec:
    values = {
        "task_id": task_id,
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "role": SubagentRole.EXPLORER,
        "title": "标题",
        "goal": "目标",
        "context": {"nested": {"value": 1}},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return SubagentSpec(**values)


def make_event(sequence: int, **overrides: object) -> SubagentEvent:
    values = {
        "event_id": f"event-{sequence}",
        "conversation_id": "conv-1",
        "parent_turn_id": "turn-1",
        "task_id": "task-1",
        "sequence": sequence,
        "role": SubagentRole.EXPLORER,
        "type": "progress",
        "payload": {"sequence": sequence, "nested": {"ok": True}},
        "timestamp": NOW,
    }
    values.update(overrides)
    return SubagentEvent(**values)


def create_legacy_database(
    db_path,
    *,
    event_sequence: int,
    event_sequences: tuple[int, ...],
    reservation_sequence: int | None = None,
) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE subagent_tasks (
            task_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            parent_turn_id TEXT NOT NULL,
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            goal TEXT NOT NULL,
            context_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            previous_status TEXT,
            error_category TEXT,
            event_sequence INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE subagent_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            conversation_id TEXT NOT NULL,
            parent_turn_id TEXT NOT NULL,
            role TEXT NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            UNIQUE(task_id, sequence)
        );
        """
    )
    connection.execute(
        """
        INSERT INTO subagent_tasks (
            task_id, conversation_id, parent_turn_id, role, title, goal,
            context_json, status, attempt, created_at, updated_at, event_sequence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            "conv-1",
            "turn-1",
            "explorer",
            "标题",
            "目标",
            "{}",
            "created",
            1,
            NOW,
            NOW,
            event_sequence,
        ),
    )
    for sequence in event_sequences:
        connection.execute(
            """
            INSERT INTO subagent_events (
                event_id, task_id, sequence, conversation_id, parent_turn_id,
                role, type, payload_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{sequence}",
                "task-1",
                sequence,
                "conv-1",
                "turn-1",
                "explorer",
                "progress",
                "{}",
                NOW,
            ),
        )
    if reservation_sequence is not None:
        connection.executescript(
            """
            CREATE TABLE subagent_event_reservations (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO subagent_event_reservations (
                task_id, sequence, token, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("task-1", reservation_sequence, "legacy-token", NOW),
        )
    connection.commit()
    connection.close()


def test_initialization_backfills_watermark_from_legacy_events(tmp_path) -> None:
    db_path = tmp_path / "subagents.db"
    create_legacy_database(
        db_path,
        event_sequence=0,
        event_sequences=(1, 3),
    )

    store = SubagentStore(db_path)

    assert store.next_sequence("task-1") == 4
    assert [event.sequence for event in store.list_events("task-1")] == [1, 3]


def test_watermark_migration_includes_reservations_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "subagents.db"
    create_legacy_database(
        db_path,
        event_sequence=1,
        event_sequences=(2,),
        reservation_sequence=5,
    )

    store = SubagentStore(db_path)
    assert store._connection.execute(
        "SELECT event_sequence FROM subagent_tasks WHERE task_id = ?",
        ("task-1",),
    ).fetchone()[0] == 5
    store.close()

    reopened = SubagentStore(db_path)

    assert reopened.next_sequence("task-1") == 6
    assert [event.sequence for event in reopened.list_events("task-1")] == [2]
    assert reopened._connection.execute(
        """
        SELECT COUNT(*) FROM subagent_event_reservations
        WHERE task_id = ? AND sequence = ?
        """,
        ("task-1", 5),
    ).fetchone()[0] == 1


def test_store_round_trip_create_get_list_and_update(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    spec = make_spec()

    store.create_task(spec)
    loaded = store.get_task(spec.task_id)

    assert loaded == spec
    assert loaded is not None
    assert loaded.role is SubagentRole.EXPLORER
    assert loaded.status is SubagentStatus.CREATED
    assert store.list_tasks("conv-1") == [spec]
    assert store.list_tasks("other") == []

    updated = replace(
        spec,
        status=SubagentStatus.RUNNING,
        previous_status=SubagentStatus.QUEUED,
        started_at=NOW,
        updated_at="2026-08-12T12:01:00+00:00",
        error_category="retryable",
    )
    store.update_task(updated)

    assert store.get_task(spec.task_id) == updated
    assert store.list_tasks(statuses=[SubagentStatus.RUNNING]) == [updated]


def test_duplicate_create_unknown_get_and_unknown_update(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())

    with pytest.raises(SubagentStoreError, match="already exists"):
        store.create_task(make_spec())
    assert store.get_task("missing") is None
    with pytest.raises(SubagentStoreError, match="not found"):
        store.update_task(make_spec("missing"))


def test_invalid_json_row_raises_safe_error_without_logging_data(tmp_path, caplog) -> None:
    db_path = tmp_path / "subagents.db"
    store = SubagentStore(db_path)
    store.create_task(make_spec(context={"token": "sk-secret-value"}))
    store.close()
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE subagent_tasks SET context_json = ? WHERE task_id = ?",
        ('{"token":"sk-secret-value"', "task-1"),
    )
    connection.commit()
    connection.close()

    store = SubagentStore(db_path)
    with pytest.raises(SubagentStoreError, match="Stored task data is invalid"):
        store.get_task("task-1")
    assert "sk-secret-value" not in caplog.text


def test_next_sequence_is_atomic_unique_and_contiguous(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())

    with ThreadPoolExecutor(max_workers=12) as pool:
        sequences = list(pool.map(lambda _: store.next_sequence("task-1"), range(100)))

    assert sorted(sequences) == list(range(1, 101))
    with pytest.raises(SubagentStoreError, match="not found"):
        store.next_sequence("missing")


def test_direct_append_advances_sequence_watermark(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())

    store.append_event(make_event(1))

    assert store.next_sequence("task-1") == 2


def test_reserved_sequence_can_be_appended_without_conflict(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())

    reservation = store.next_sequence("task-1")
    assert reservation == 1
    store.append_event(make_event(reservation))

    assert store.next_sequence("task-1") == 2
    with pytest.raises(SubagentStoreError, match="sequence"):
        store.append_event(make_event(1, event_id="event-other"))


def test_sequence_reservation_cannot_be_consumed_without_owner_token(tmp_path) -> None:
    db_path = tmp_path / "subagents.db"
    first = SubagentStore(db_path)
    second = SubagentStore(db_path)
    first.create_task(make_spec())

    reservation = first.next_sequence("task-1")

    assert reservation == 1
    assert reservation.token
    with pytest.raises(SubagentStoreError, match="reservation"):
        second.append_event(make_event(int(reservation)))

    second.append_event(make_event(reservation))
    assert second.list_events("task-1") == [make_event(1)]


def test_update_rejects_immutable_task_identity_across_instances(tmp_path) -> None:
    db_path = tmp_path / "subagents.db"
    first = SubagentStore(db_path)
    second = SubagentStore(db_path)
    spec = make_spec()
    first.create_task(spec)

    changed_identity = replace(
        spec,
        conversation_id="other-conversation",
        parent_turn_id="other-turn",
        role=SubagentRole.OPERATOR,
    )

    with pytest.raises(SubagentStoreError, match="identity"):
        second.update_task(changed_identity)
    assert first.get_task("task-1") == spec


def test_concurrent_append_and_identity_change_cannot_cross_conversations(tmp_path) -> None:
    db_path = tmp_path / "subagents.db"
    first = SubagentStore(db_path)
    second = SubagentStore(db_path)
    spec = make_spec()
    first.create_task(spec)
    barrier = threading.Barrier(2)

    def append() -> None:
        barrier.wait()
        first.append_event(make_event(1))

    def change_identity() -> None:
        barrier.wait()
        with pytest.raises(SubagentStoreError, match="identity"):
            second.update_task(replace(spec, conversation_id="other"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        append_future = pool.submit(append)
        update_future = pool.submit(change_identity)
        append_future.result()
        update_future.result()

    assert first.list_events("task-1") == [make_event(1)]
    assert first.get_task("task-1") == spec


def test_sqlite_enables_foreign_keys_and_wal_for_file_database(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")

    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_append_and_list_events_are_ordered_and_validated(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())
    event_two = make_event(2)
    event_one = make_event(1)

    store.append_event(event_one)
    store.append_event(event_two)

    assert store.list_events("task-1") == [event_one, event_two]
    assert store.list_events("task-1", after_sequence=1, limit=1) == [event_two]
    with pytest.raises(SubagentStoreError, match="sequence"):
        store.append_event(make_event(1, event_id="duplicate-sequence"))
    with pytest.raises(SubagentStoreError, match="does not match"):
        store.append_event(make_event(3, task_id="task-2"), task_id="task-1")


def test_mark_nonterminal_interrupted_preserves_previous_status_atomically(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    statuses = list(SubagentStatus)
    for index, status in enumerate(statuses):
        previous = SubagentStatus.RUNNING if status is SubagentStatus.INTERRUPTED else None
        store.create_task(
            make_spec(
                f"task-{index}",
                status=status,
                previous_status=previous,
                conversation_id=f"conv-{index}",
            )
        )

    changed = store.mark_nonterminal_interrupted()
    changed_by_id = {spec.task_id: spec for spec in changed}

    expected_changed = {
        status
        for status in statuses
        if status
        not in {
            SubagentStatus.COMPLETED,
            SubagentStatus.FAILED,
            SubagentStatus.CANCELLED,
            SubagentStatus.INTERRUPTED,
        }
    }
    assert {spec.previous_status for spec in changed} == expected_changed
    for index, original_status in enumerate(statuses):
        loaded = store.get_task(f"task-{index}")
        assert loaded is not None
        if original_status in expected_changed:
            assert loaded.status is SubagentStatus.INTERRUPTED
            assert loaded.previous_status is original_status
            assert loaded.task_id in changed_by_id
        elif original_status is SubagentStatus.INTERRUPTED:
            assert loaded.previous_status is SubagentStatus.RUNNING
        else:
            assert loaded.status is original_status


def test_close_is_idempotent_and_thread_safe(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.close(), range(20)))
    store.close()


def test_initialization_failure_closes_open_connection(tmp_path, monkeypatch) -> None:
    captured = {}

    def fail_schema(store) -> None:
        captured["connection"] = store._connection
        raise sqlite3.OperationalError("schema failed")

    monkeypatch.setattr(SubagentStore, "_create_schema", fail_schema)

    with pytest.raises(SubagentStoreError, match="initialize"):
        SubagentStore(tmp_path / "subagents.db")

    with pytest.raises(sqlite3.ProgrammingError):
        captured["connection"].execute("SELECT 1")


def test_invalid_reservation_type_fails_safely_without_holding_transaction(tmp_path) -> None:
    """A plain-int reservation must be rejected before BEGIN, leaving no open write lock."""
    db = tmp_path / "subagents.db"
    store = SubagentStore(db)
    store.create_task(make_spec())

    with pytest.raises(SubagentStoreError, match="reservation"):
        store.append_event(make_event(1), reservation=1)  # type: ignore[arg-type]
    assert store._connection.in_transaction is False

    # A second instance can write immediately — no lingering write lock.
    other = SubagentStore(db)
    other.append_event(make_event(1))
    assert [event.sequence for event in other.list_events("task-1")] == [1]
    other.close()
    store.close()


def test_unexpected_error_inside_append_transaction_rolls_back(tmp_path, monkeypatch) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())
    reservation = store.next_sequence("task-1")

    # Break token comparison to simulate an unexpected non-sqlite error mid-transaction.
    monkeypatch.setattr(
        "agent_assistant.subagents.store.secrets.compare_digest",
        lambda *_: (_ for _ in ()).throw(TypeError("boom")),
    )
    with pytest.raises(TypeError):
        store.append_event(make_event(int(reservation)), reservation=reservation)
    assert store._connection.in_transaction is False
    store.close()


def test_mark_nonterminal_interrupted_leaves_no_open_transaction(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec("task-a", status=SubagentStatus.RUNNING))
    store.create_task(
        make_spec("task-b", status=SubagentStatus.COMPLETED, conversation_id="conv-2")
    )

    changed = store.mark_nonterminal_interrupted()
    assert [spec.task_id for spec in changed] == ["task-a"]
    assert store._connection.in_transaction is False
    completed = store.get_task("task-b")
    assert completed is not None and completed.status is SubagentStatus.COMPLETED
    store.close()


def test_sequence_reservation_pickle_round_trip_preserves_token(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    store.create_task(make_spec())
    reservation = store.next_sequence("task-1")

    restored = pickle.loads(pickle.dumps(reservation))
    assert isinstance(restored, SequenceReservation)
    assert int(restored) == int(reservation)
    assert restored.token == reservation.token

    # The restored reservation is still consumable.
    store.append_event(make_event(int(restored)), reservation=restored)
    assert [event.sequence for event in store.list_events("task-1")] == [1]
    store.close()


def test_close_failure_keeps_store_retryable(tmp_path) -> None:
    store = SubagentStore(tmp_path / "subagents.db")
    real_connection = store._connection

    class FailOnceConnection:
        attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise sqlite3.OperationalError("close failed")
            real_connection.close()

    wrapper = FailOnceConnection()
    store._connection = wrapper

    with pytest.raises(SubagentStoreError, match="close"):
        store.close()
    assert store._closed is False

    store.close()
    assert store._closed is True
    assert wrapper.attempts == 2

