"""Thread-safe SQLite persistence for validated subagent task contracts."""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import SubagentEvent, SubagentSpec, SubagentStatus

logger = logging.getLogger(__name__)


class SubagentStoreError(RuntimeError):
    """Caller-safe persistence error without database internals."""


class SequenceReservation(int):
    """Integer-compatible event sequence carrying an unforgeable reservation token."""

    token: str

    def __new__(cls, sequence: int, token: str) -> SequenceReservation:
        instance = int.__new__(cls, sequence)
        instance.token = token
        return instance

    def __reduce__(self) -> tuple[type, tuple[int, str]]:
        # int.__new__ pickling would drop the token; rebuild with both args.
        return (self.__class__, (int(self), self.token))

    @property
    def sequence(self) -> int:
        return int(self)


class SubagentStore:
    """Persist subagent tasks and ordered events through one locked connection."""

    def __init__(self, db_path: str | Path) -> None:
        path_text = str(db_path)
        self._lock = threading.RLock()
        self._closed = False
        connection: sqlite3.Connection | None = None
        try:
            if path_text != ":memory:":
                Path(path_text).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path_text, check_same_thread=False)
            self._connection = connection
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            foreign_keys = self._connection.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or foreign_keys[0] != 1:
                raise SubagentStoreError("Unable to enable SQLite foreign keys")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            try:
                journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                    logger.warning("SQLite WAL mode unavailable; using safe fallback")
            except sqlite3.DatabaseError:
                logger.warning("SQLite WAL mode unavailable; using safe fallback")
            self._create_schema()
        except (OSError, sqlite3.Error, SubagentStoreError):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise SubagentStoreError("Unable to initialize subagent store") from None

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS subagent_tasks (
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
            CREATE INDEX IF NOT EXISTS idx_subagent_tasks_conversation
                ON subagent_tasks(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_subagent_tasks_status
                ON subagent_tasks(status);
            CREATE TABLE IF NOT EXISTS subagent_events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                parent_turn_id TEXT NOT NULL,
                role TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES subagent_tasks(task_id) ON DELETE CASCADE,
                UNIQUE(task_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_subagent_events_task_sequence
                ON subagent_events(task_id, sequence);
            CREATE TABLE IF NOT EXISTS subagent_event_reservations (
                task_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(task_id, sequence),
                FOREIGN KEY(task_id) REFERENCES subagent_tasks(task_id) ON DELETE CASCADE
            );
            UPDATE subagent_tasks
            SET event_sequence = MAX(
                event_sequence,
                COALESCE(
                    (
                        SELECT MAX(subagent_events.sequence)
                        FROM subagent_events
                        WHERE subagent_events.task_id = subagent_tasks.task_id
                    ),
                    0
                ),
                COALESCE(
                    (
                        SELECT MAX(subagent_event_reservations.sequence)
                        FROM subagent_event_reservations
                        WHERE subagent_event_reservations.task_id = subagent_tasks.task_id
                    ),
                    0
                )
            );
            COMMIT;
            """
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise SubagentStoreError("Subagent store is closed")

    @staticmethod
    def _task_values(spec: SubagentSpec) -> tuple[object, ...]:
        data = spec.to_dict()
        return (
            data["task_id"],
            data["conversation_id"],
            data["parent_turn_id"],
            data["role"],
            data["title"],
            data["goal"],
            json.dumps(data["context"], ensure_ascii=False, allow_nan=False),
            data["status"],
            data["attempt"],
            data["created_at"],
            data["started_at"],
            data["updated_at"],
            data["finished_at"],
            data["previous_status"],
            data["error_category"],
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> SubagentSpec:
        try:
            context = json.loads(row["context_json"])
            return SubagentSpec(
                task_id=row["task_id"],
                conversation_id=row["conversation_id"],
                parent_turn_id=row["parent_turn_id"],
                role=row["role"],
                title=row["title"],
                goal=row["goal"],
                context=context,
                status=row["status"],
                attempt=row["attempt"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                finished_at=row["finished_at"],
                previous_status=row["previous_status"],
                error_category=row["error_category"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Rejected invalid stored subagent task data")
            raise SubagentStoreError("Stored task data is invalid") from None

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> SubagentEvent:
        try:
            payload = json.loads(row["payload_json"])
            return SubagentEvent(
                event_id=row["event_id"],
                conversation_id=row["conversation_id"],
                parent_turn_id=row["parent_turn_id"],
                task_id=row["task_id"],
                sequence=row["sequence"],
                role=row["role"],
                type=row["type"],
                payload=payload,
                timestamp=row["timestamp"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Rejected invalid stored subagent event data")
            raise SubagentStoreError("Stored event data is invalid") from None

    def create_task(self, spec: SubagentSpec) -> None:
        values = self._task_values(spec)
        with self._lock:
            self._ensure_open()
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO subagent_tasks (
                            task_id, conversation_id, parent_turn_id, role, title, goal,
                            context_json, status, attempt, created_at, started_at, updated_at,
                            finished_at, previous_status, error_category
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
            except sqlite3.IntegrityError:
                raise SubagentStoreError("Subagent task already exists") from None
            except sqlite3.Error:
                raise SubagentStoreError("Unable to create subagent task") from None

    def update_task(self, spec: SubagentSpec) -> None:
        data = spec.to_dict()
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                identity = self._connection.execute(
                    """
                    SELECT conversation_id, parent_turn_id, role
                    FROM subagent_tasks
                    WHERE task_id = ?
                    """,
                    (spec.task_id,),
                ).fetchone()
                if identity is None:
                    raise SubagentStoreError("Subagent task not found")
                if (
                    identity["conversation_id"] != spec.conversation_id
                    or identity["parent_turn_id"] != spec.parent_turn_id
                    or identity["role"] != spec.role.value
                ):
                    raise SubagentStoreError("Subagent task identity is immutable")
                self._connection.execute(
                    """
                    UPDATE subagent_tasks SET
                        title = ?, goal = ?, context_json = ?, status = ?, attempt = ?,
                        created_at = ?, started_at = ?, updated_at = ?, finished_at = ?,
                        previous_status = ?, error_category = ?
                    WHERE task_id = ?
                    """,
                    (
                        data["title"],
                        data["goal"],
                        json.dumps(data["context"], ensure_ascii=False, allow_nan=False),
                        data["status"],
                        data["attempt"],
                        data["created_at"],
                        data["started_at"],
                        data["updated_at"],
                        data["finished_at"],
                        data["previous_status"],
                        data["error_category"],
                        data["task_id"],
                    ),
                )
                self._connection.commit()
            except SubagentStoreError:
                self._connection.rollback()
                raise
            except sqlite3.Error:
                self._connection.rollback()
                raise SubagentStoreError("Unable to update subagent task") from None
            except BaseException:
                self._connection.rollback()
                raise

    save_task = update_task

    def get_task(self, task_id: str) -> SubagentSpec | None:
        with self._lock:
            self._ensure_open()
            try:
                row = self._connection.execute(
                    "SELECT * FROM subagent_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            except sqlite3.Error:
                raise SubagentStoreError("Unable to read subagent task") from None
            return None if row is None else self._task_from_row(row)

    def list_tasks(
        self,
        conversation_id: str | None = None,
        statuses: Iterable[SubagentStatus | str] | None = None,
    ) -> list[SubagentSpec]:
        clauses: list[str] = []
        parameters: list[object] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            parameters.append(conversation_id)
        if statuses is not None:
            normalized = [SubagentStatus(status).value for status in statuses]
            if not normalized:
                return []
            clauses.append(f"status IN ({','.join('?' for _ in normalized)})")
            parameters.extend(normalized)
        query = "SELECT * FROM subagent_tasks"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, task_id"
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(query, parameters).fetchall()
            except sqlite3.Error:
                raise SubagentStoreError("Unable to list subagent tasks") from None
            return [self._task_from_row(row) for row in rows]

    def next_sequence(self, task_id: str) -> SequenceReservation:
        """Reserve the next monotonic sequence.

        A reservation is durable even if the corresponding event write later fails, so gaps
        are allowed. Only the returned token-bearing integer can consume that reservation.
        Direct unreserved appends raise the watermark to avoid future collisions.
        """

        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                cursor = self._connection.execute(
                    """
                    UPDATE subagent_tasks
                    SET event_sequence = event_sequence + 1
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
                if cursor.rowcount != 1:
                    raise SubagentStoreError("Subagent task not found")
                row = self._connection.execute(
                    "SELECT event_sequence FROM subagent_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise SubagentStoreError("Subagent task not found")
                sequence = int(row["event_sequence"])
                token = secrets.token_urlsafe(24)
                self._connection.execute(
                    """
                    INSERT INTO subagent_event_reservations (
                        task_id, sequence, token, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, sequence, token, datetime.now(timezone.utc).isoformat()),
                )
                self._connection.commit()
                return SequenceReservation(sequence, token)
            except SubagentStoreError:
                self._connection.rollback()
                raise
            except sqlite3.Error:
                self._connection.rollback()
                raise SubagentStoreError("Unable to reserve event sequence") from None
            except BaseException:
                self._connection.rollback()
                raise

    def append_event(
        self,
        event: SubagentEvent,
        *,
        task_id: str | None = None,
        reservation: SequenceReservation | None = None,
    ) -> None:
        """Atomically validate identity, consume sequence ownership, and append an event."""

        target_task_id = event.task_id if task_id is None else task_id
        if event.task_id != target_task_id:
            raise SubagentStoreError("Event task_id does not match target task")
        if reservation is not None and not isinstance(reservation, SequenceReservation):
            raise SubagentStoreError("reservation must be a SequenceReservation")
        sequence_reservation = (
            reservation
            if reservation is not None
            else event.sequence
            if isinstance(event.sequence, SequenceReservation)
            else None
        )
        if sequence_reservation is not None:
            token = getattr(sequence_reservation, "token", None)
            if not isinstance(token, str) or not token:
                raise SubagentStoreError("Event sequence reservation is invalid")
            if int(sequence_reservation) != event.sequence:
                raise SubagentStoreError("Event sequence reservation does not match event")
        data = event.to_dict()
        with self._lock:
            self._ensure_open()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                task = self._connection.execute(
                    """
                    SELECT conversation_id, parent_turn_id, role, event_sequence
                    FROM subagent_tasks
                    WHERE task_id = ?
                    """,
                    (target_task_id,),
                ).fetchone()
                if task is None:
                    raise SubagentStoreError("Subagent task not found")
                if (
                    event.conversation_id != task["conversation_id"]
                    or event.parent_turn_id != task["parent_turn_id"]
                    or event.role.value != task["role"]
                ):
                    raise SubagentStoreError("Event identity does not match target task")

                if sequence_reservation is not None:
                    stored = self._connection.execute(
                        """
                        SELECT token FROM subagent_event_reservations
                        WHERE task_id = ? AND sequence = ?
                        """,
                        (target_task_id, event.sequence),
                    ).fetchone()
                    if stored is None or not secrets.compare_digest(
                        stored["token"],
                        sequence_reservation.token,
                    ):
                        raise SubagentStoreError("Event sequence reservation is invalid")
                    self._connection.execute(
                        """
                        DELETE FROM subagent_event_reservations
                        WHERE task_id = ? AND sequence = ?
                        """,
                        (target_task_id, event.sequence),
                    )
                elif event.sequence != int(task["event_sequence"]) + 1:
                    raise SubagentStoreError("Event sequence reservation is required")

                self._connection.execute(
                    """
                    INSERT INTO subagent_events (
                        event_id, task_id, sequence, conversation_id, parent_turn_id,
                        role, type, payload_json, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["event_id"],
                        data["task_id"],
                        int(event.sequence),
                        data["conversation_id"],
                        data["parent_turn_id"],
                        data["role"],
                        data["type"],
                        json.dumps(data["payload"], ensure_ascii=False, allow_nan=False),
                        data["timestamp"],
                    ),
                )
                self._connection.execute(
                    """
                    UPDATE subagent_tasks
                    SET event_sequence = CASE
                        WHEN event_sequence < ? THEN ?
                        ELSE event_sequence
                    END
                    WHERE task_id = ?
                    """,
                    (event.sequence, event.sequence, target_task_id),
                )
                self._connection.commit()
            except SubagentStoreError:
                self._connection.rollback()
                raise
            except sqlite3.IntegrityError:
                self._connection.rollback()
                raise SubagentStoreError("Event id or sequence already exists") from None
            except sqlite3.Error:
                self._connection.rollback()
                raise SubagentStoreError("Unable to append subagent event") from None
            except BaseException:
                # Never leave the write transaction open on unexpected errors.
                self._connection.rollback()
                raise

    def list_events(
        self,
        task_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[SubagentEvent]:
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool):
            raise ValueError("after_sequence must be an integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._connection.execute(
                    """
                    SELECT * FROM subagent_events
                    WHERE task_id = ? AND sequence > ?
                    ORDER BY sequence
                    LIMIT ?
                    """,
                    (task_id, after_sequence, limit),
                ).fetchall()
            except sqlite3.Error:
                raise SubagentStoreError("Unable to list subagent events") from None
            return [self._event_from_row(row) for row in rows]

    def mark_nonterminal_interrupted(self) -> list[SubagentSpec]:
        interruptible = (
            SubagentStatus.CREATED,
            SubagentStatus.QUEUED,
            SubagentStatus.RUNNING,
            SubagentStatus.WAITING_USER,
            SubagentStatus.PAUSED,
        )
        placeholders = ",".join("?" for _ in interruptible)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                # BEGIN IMMEDIATE takes the write lock before the SELECT, so no
                # other connection can flip a task to a terminal state between
                # the snapshot and the UPDATE below.
                self._connection.execute("BEGIN IMMEDIATE")
                rows = self._connection.execute(
                    f"""
                    SELECT * FROM subagent_tasks
                    WHERE status IN ({placeholders})
                    ORDER BY created_at, task_id
                    """,
                    tuple(status.value for status in interruptible),
                ).fetchall()
                changed: list[SubagentSpec] = []
                for row in rows:
                    spec = replace(
                        self._task_from_row(row),
                        status=SubagentStatus.INTERRUPTED,
                        previous_status=SubagentStatus(row["status"]),
                        updated_at=now,
                    )
                    # Status guard is defense in depth: only flip the row if it
                    # still holds the exact status we snapshotted.
                    cursor = self._connection.execute(
                        """
                        UPDATE subagent_tasks
                        SET status = ?, previous_status = ?, updated_at = ?
                        WHERE task_id = ? AND status = ?
                        """,
                        (
                            SubagentStatus.INTERRUPTED.value,
                            row["status"],
                            now,
                            row["task_id"],
                            row["status"],
                        ),
                    )
                    if cursor.rowcount == 1:
                        changed.append(spec)
                self._connection.commit()
                return changed
            except SubagentStoreError:
                self._connection.rollback()
                raise
            except sqlite3.Error:
                self._connection.rollback()
                raise SubagentStoreError("Unable to interrupt subagent tasks") from None
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.close()
            except sqlite3.Error:
                raise SubagentStoreError("Unable to close subagent store") from None
            self._closed = True

