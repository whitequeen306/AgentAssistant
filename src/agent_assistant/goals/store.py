"""SQLite persistence for goal tracks and their milestones.

Tables:
- goal_tracks(track_id, kind, title, status, cycle_year, config_json,
              created_at, updated_at)
- goal_milestones(track_id, key, label, due_at, done, note)

Thread-safe: bridge calls arrive from the webview thread and background worker
threads, so every access goes through a single lock (same model as
``ui/store.py`` and ``practice/store.py``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_assistant.goals.models import (
    GoalTrack,
    TrackMilestone,
    TrackStatus,
    infer_cycle_year,
)
from agent_assistant.goals.registry import get_spec

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_tracks (
    track_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    cycle_year  INTEGER,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_tracks_status ON goal_tracks(status);

CREATE TABLE IF NOT EXISTS goal_milestones (
    track_id TEXT NOT NULL REFERENCES goal_tracks(track_id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    label    TEXT NOT NULL DEFAULT '',
    due_at   REAL NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0,
    note     TEXT NOT NULL DEFAULT '',
    -- 用户手改过日期后钉住，之后 sync 不再覆盖（改 cycle_year 时同理）
    pinned   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, key)
);
CREATE INDEX IF NOT EXISTS idx_goal_milestones_due ON goal_milestones(due_at);
"""


def _default_db_path() -> Path:
    from agent_assistant.config import settings

    return settings.goals_db_path


class GoalStore:
    """SQLite-backed store for goal tracks and materialized milestones."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _ensure_db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ─── Tracks ────────────────────────────────────────────────────

    def create_track(
        self,
        *,
        kind: str,
        title: str = "",
        cycle_year: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> GoalTrack:
        """Create a track and materialize its default milestones."""
        track_id = uuid.uuid4().hex[:12]
        now = time.time()
        year = cycle_year if cycle_year is not None else infer_cycle_year()
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO goal_tracks (track_id, kind, title, status,"
                " cycle_year, config_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    track_id,
                    kind,
                    title.strip(),
                    TrackStatus.ACTIVE,
                    year,
                    json.dumps(config or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        self.sync_milestones(track_id)
        return self.get_track(track_id) or GoalTrack(
            track_id=track_id, kind=kind, title=title, cycle_year=year,
            created_at=now, updated_at=now,
        )

    def _row_to_track(self, row: sqlite3.Row) -> GoalTrack:
        try:
            config = json.loads(row["config_json"] or "{}")
        except json.JSONDecodeError:
            config = {}
        return GoalTrack(
            track_id=row["track_id"],
            kind=row["kind"],
            title=row["title"],
            status=row["status"],
            cycle_year=row["cycle_year"],
            config=config if isinstance(config, dict) else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_track(self, track_id: str) -> GoalTrack | None:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM goal_tracks WHERE track_id = ?", (track_id,)
            ).fetchone()
        return self._row_to_track(row) if row else None

    def list_tracks(self, status: str | None = None) -> list[GoalTrack]:
        with self._lock:
            conn = self._ensure_db()
            if status:
                rows = conn.execute(
                    "SELECT * FROM goal_tracks WHERE status = ?"
                    " ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM goal_tracks ORDER BY created_at DESC"
                ).fetchall()
        return [self._row_to_track(r) for r in rows]

    def active_tracks(self) -> list[GoalTrack]:
        return self.list_tracks(TrackStatus.ACTIVE)

    def update_track(self, track_id: str, **fields: Any) -> GoalTrack | None:
        """Patch title / cycle_year / config / status. Unknown keys ignored."""
        allowed = {"title", "cycle_year", "config", "status"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "config":
                if not isinstance(value, dict):
                    continue
                updates.append("config_json = ?")
                values.append(json.dumps(value, ensure_ascii=False))
            else:
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            return self.get_track(track_id)

        updates.append("updated_at = ?")
        values.append(time.time())
        values.append(track_id)
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                f"UPDATE goal_tracks SET {', '.join(updates)} WHERE track_id = ?",
                values,
            )
            conn.commit()
        # 周期年份变了 → 按新年份补齐/校正节点
        if "cycle_year" in fields:
            self.sync_milestones(track_id)
        return self.get_track(track_id)

    def archive_track(self, track_id: str) -> GoalTrack | None:
        return self.update_track(track_id, status=TrackStatus.ARCHIVED)

    def delete_track(self, track_id: str) -> bool:
        with self._lock:
            conn = self._ensure_db()
            cur = conn.execute(
                "DELETE FROM goal_tracks WHERE track_id = ?", (track_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    # ─── Milestones ────────────────────────────────────────────────

    def sync_milestones(self, track_id: str) -> int:
        """Upsert milestone rows from the spec defaults for the track's year.

        Unpinned rows follow the spec (so changing ``cycle_year`` moves the
        whole timeline); rows whose date the user edited by hand (``pinned``)
        are left alone. ``done`` and ``note`` are never touched.

        Returns the number of rows written (inserted + updated).
        """
        track = self.get_track(track_id)
        if track is None or track.cycle_year is None:
            return 0
        spec = get_spec(track.kind)
        if spec is None:
            return 0

        written = 0
        with self._lock:
            conn = self._ensure_db()
            for m in spec.milestones:
                d = m.default_date(track.cycle_year)
                due = datetime(d.year, d.month, d.day).timestamp()
                cur = conn.execute(
                    "INSERT INTO goal_milestones"
                    " (track_id, key, label, due_at, done, note, pinned)"
                    " VALUES (?, ?, ?, ?, 0, ?, 0)"
                    " ON CONFLICT(track_id, key) DO UPDATE SET"
                    "  label = excluded.label, due_at = excluded.due_at"
                    " WHERE goal_milestones.pinned = 0",
                    (track_id, m.key, m.label, due, m.note),
                )
                written += cur.rowcount
            conn.commit()
        return written

    def list_milestones(self, track_id: str) -> list[TrackMilestone]:
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT * FROM goal_milestones WHERE track_id = ? ORDER BY due_at",
                (track_id,),
            ).fetchall()
        return [
            TrackMilestone(
                track_id=r["track_id"],
                key=r["key"],
                label=r["label"],
                due_at=r["due_at"],
                done=bool(r["done"]),
                note=r["note"],
            )
            for r in rows
        ]

    def set_milestone(
        self,
        track_id: str,
        key: str,
        *,
        due_at: float | None = None,
        done: bool | None = None,
        note: str | None = None,
    ) -> TrackMilestone | None:
        updates: list[str] = []
        values: list[Any] = []
        if due_at is not None:
            # 手改日期 → 钉住，后续 sync（含改年份）不再覆盖
            updates.append("due_at = ?")
            updates.append("pinned = 1")
            values.append(float(due_at))
        if done is not None:
            updates.append("done = ?")
            values.append(1 if done else 0)
        if note is not None:
            updates.append("note = ?")
            values.append(note)
        if not updates:
            return self._get_milestone(track_id, key)

        values.extend([track_id, key])
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                f"UPDATE goal_milestones SET {', '.join(updates)}"
                " WHERE track_id = ? AND key = ?",
                values,
            )
            conn.commit()
        return self._get_milestone(track_id, key)

    def _get_milestone(self, track_id: str, key: str) -> TrackMilestone | None:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM goal_milestones WHERE track_id = ? AND key = ?",
                (track_id, key),
            ).fetchone()
        if not row:
            return None
        return TrackMilestone(
            track_id=row["track_id"],
            key=row["key"],
            label=row["label"],
            due_at=row["due_at"],
            done=bool(row["done"]),
            note=row["note"],
        )

    def due_milestones(
        self, within_days: int = 7, now: float | None = None
    ) -> list[tuple[GoalTrack, TrackMilestone]]:
        """Milestones due within ``within_days`` (plus already-overdue ones)."""
        ts = time.time() if now is None else now
        horizon = ts + within_days * 86400
        out: list[tuple[GoalTrack, TrackMilestone]] = []
        for track in self.active_tracks():
            for m in self.list_milestones(track.track_id):
                if m.done:
                    continue
                if m.due_at <= horizon:
                    out.append((track, m))
        return sorted(out, key=lambda pair: pair[1].due_at)


class _LazyStore:
    """Defer db path resolution until first use (tests can swap instance)."""

    def __init__(self) -> None:
        self._store: GoalStore | None = None

    def _get(self) -> GoalStore:
        if self._store is None:
            self._store = GoalStore(_default_db_path())
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


# Singleton (lazy)
goal_store: Any = _LazyStore()

__all__ = ["GoalStore", "goal_store"]
