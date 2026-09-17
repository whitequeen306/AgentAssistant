"""SQLite persistence for the practice room.

Tables:
- sessions(id, created_at, source_kind, source_id, source_title, title, mode,
           question_count, correct_count, score_pct, submitted)
- questions(id, type, question, options_json, answer, explain, difficulty)
- session_questions(session_id, question_id, ord)  — quiz composition
- attempts(id, question_id, session_id, answered_at, user_answer, score,
           is_correct, feedback)
- cards(card_id=question_id, box, due_at, total_attempts, correct_attempts,
        created_at)  — Leitner scheduling state, 1:1 with questions

Thread-safe: bridge calls arrive from the webview thread and background
worker threads, so every access goes through a single lock (same model as
ui/store.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from agent_assistant.practice.scheduler import (
    CORRECT_SCORE_THRESHOLD,
    clamp_box,
    next_state,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    created_at     REAL NOT NULL,
    source_kind    TEXT NOT NULL,
    source_id      TEXT NOT NULL DEFAULT '',
    source_title   TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    mode           TEXT NOT NULL DEFAULT 'paper',
    question_count INTEGER NOT NULL DEFAULT 0,
    correct_count  INTEGER,
    score_pct      INTEGER,
    submitted      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS questions (
    question_id  TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT,
    answer       TEXT NOT NULL,
    explain      TEXT,
    difficulty   TEXT,
    category     TEXT
);
CREATE TABLE IF NOT EXISTS session_questions (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    question_id TEXT NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    ord         INTEGER NOT NULL,
    PRIMARY KEY (session_id, question_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    session_id  TEXT NOT NULL,
    answered_at REAL NOT NULL,
    user_answer TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    is_correct  INTEGER NOT NULL DEFAULT 0,
    feedback    TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_question ON attempts(question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id);
CREATE TABLE IF NOT EXISTS cards (
    card_id          TEXT PRIMARY KEY,
    question_id      TEXT UNIQUE NOT NULL REFERENCES questions(question_id) ON DELETE CASCADE,
    box              INTEGER NOT NULL DEFAULT 1,
    due_at           REAL NOT NULL,
    total_attempts   INTEGER NOT NULL DEFAULT 0,
    correct_attempts INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(due_at);
"""


class PracticeStore:
    """SQLite-backed store for practice sessions, questions and scheduling."""

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
            self._migrate()
            self._conn.commit()
        return self._conn

    def _migrate(self) -> None:
        """Lightweight column migrations for DBs created before 2.1."""
        assert self._conn is not None
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(questions)").fetchall()
        }
        if "category" not in cols:
            self._conn.execute("ALTER TABLE questions ADD COLUMN category TEXT")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ─── Sessions ──────────────────────────────────────────────────

    def create_session(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_title: str,
        title: str,
        mode: str = "paper",
        question_count: int = 0,
    ) -> dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        import time as _time

        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO sessions (session_id, created_at, source_kind,"
                " source_id, source_title, title, mode, question_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    _time.time(),
                    source_kind,
                    source_id,
                    source_title,
                    title,
                    mode,
                    question_count,
                ),
            )
            conn.commit()
        return {"session_id": session_id}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def finalize_session(
        self, session_id: str, *, correct_count: int, score_pct: int
    ) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "UPDATE sessions SET correct_count = ?, score_pct = ?,"
                " submitted = 1 WHERE session_id = ?",
                (correct_count, score_pct, session_id),
            )
            conn.commit()

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Submitted sessions, newest first."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT * FROM sessions WHERE submitted = 1"
                " ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Questions ─────────────────────────────────────────────────

    def add_question(
        self,
        *,
        question_id: str,
        type: str,  # noqa: A002 — mirrors the JSON schema field name
        question: str,
        options: list[str] | None,
        answer: str,
        explain: str,
        difficulty: str = "",
        category: str = "",
    ) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT OR IGNORE INTO questions (question_id, type, question,"
                " options_json, answer, explain, difficulty, category)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    question_id,
                    type,
                    question,
                    json.dumps(options, ensure_ascii=False) if options else None,
                    answer,
                    explain,
                    difficulty,
                    category,
                ),
            )
            conn.commit()

    def link_questions(
        self, session_id: str, question_ids: list[str]
    ) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.executemany(
                "INSERT OR REPLACE INTO session_questions"
                " (session_id, question_id, ord) VALUES (?, ?, ?)",
                [
                    (session_id, qid, idx)
                    for idx, qid in enumerate(question_ids)
                ],
            )
            conn.commit()

    def get_session_questions(self, session_id: str) -> list[dict[str, Any]]:
        """Questions of a session in quiz order."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT q.*, sq.ord FROM session_questions sq"
                " JOIN questions q ON q.question_id = sq.question_id"
                " WHERE sq.session_id = ? ORDER BY sq.ord",
                (session_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = (
                json.loads(d.pop("options_json"))
                if d.get("options_json")
                else None
            )
            out.append(d)
        return out

    def get_question(self, question_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM questions WHERE question_id = ?", (question_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["options"] = (
            json.loads(d.pop("options_json")) if d.get("options_json") else None
        )
        return d

    def get_wrong_questions(self, session_id: str) -> list[dict[str, Any]]:
        """Questions the user got wrong in a session (last attempt per question)."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                """
                SELECT q.* FROM questions q
                JOIN attempts a
                  ON a.question_id = q.question_id AND a.session_id = ?
                WHERE a.rowid = (
                          SELECT MAX(rowid) FROM attempts
                          WHERE session_id = ? AND question_id = q.question_id
                      )
                  AND a.score < ?
                """,
                (session_id, session_id, CORRECT_SCORE_THRESHOLD),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = (
                json.loads(d.pop("options_json"))
                if d.get("options_json")
                else None
            )
            out.append(d)
        return out

    # ─── Attempts ──────────────────────────────────────────────────

    def insert_attempt(
        self,
        *,
        question_id: str,
        session_id: str,
        user_answer: str,
        score: float,
        feedback: str = "",
    ) -> str:
        import time as _time

        attempt_id = uuid.uuid4().hex[:12]
        is_correct = 1 if score >= CORRECT_SCORE_THRESHOLD else 0
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO attempts (attempt_id, question_id, session_id,"
                " answered_at, user_answer, score, is_correct, feedback)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    question_id,
                    session_id,
                    _time.time(),
                    user_answer,
                    float(score),
                    is_correct,
                    feedback,
                ),
            )
            conn.commit()
        return attempt_id

    def get_session_attempts(self, session_id: str) -> list[dict[str, Any]]:
        """All attempts of a session (one per question, insertion order)."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT * FROM attempts WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Cards (Leitner) ───────────────────────────────────────────

    def upsert_card_for_attempt(
        self, question_id: str, *, correct: bool
    ) -> dict[str, Any]:
        """Record an attempt on the card and advance its Leitner state."""
        import time as _time

        now = _time.time()
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT box FROM cards WHERE question_id = ?", (question_id,)
            ).fetchone()
            if row:
                new_box, due_at = next_state(row["box"], correct, now)
                conn.execute(
                    "UPDATE cards SET box = ?, due_at = ?,"
                    " total_attempts = total_attempts + 1,"
                    " correct_attempts = correct_attempts + ?"
                    " WHERE question_id = ?",
                    (new_box, due_at, 1 if correct else 0, question_id),
                )
            else:
                new_box, due_at = next_state(1, correct, now)
                conn.execute(
                    "INSERT INTO cards (card_id, question_id, box, due_at,"
                    " total_attempts, correct_attempts, created_at)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (
                        question_id,
                        question_id,
                        new_box,
                        due_at,
                        1 if correct else 0,
                        now,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM cards WHERE question_id = ?", (question_id,)
            ).fetchone()
        return dict(row) if row else {}

    def due_cards(self, limit: int = 20) -> list[dict[str, Any]]:
        """Due cards, most overdue first."""
        import time as _time

        now = _time.time()
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT c.*, q.type, q.question, q.options_json, q.answer,"
                " q.explain, q.difficulty"
                " FROM cards c JOIN questions q ON q.question_id = c.question_id"
                " WHERE c.due_at <= ?"
                " ORDER BY c.due_at ASC LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = (
                json.loads(d.pop("options_json"))
                if d.get("options_json")
                else None
            )
            out.append(d)
        return out

    def due_info(self) -> dict[str, Any]:
        """Due count + per-box distribution for the homepage card."""
        import time as _time

        now = _time.time()
        with self._lock:
            conn = self._ensure_db()
            due = conn.execute(
                "SELECT COUNT(*) AS n FROM cards WHERE due_at <= ?", (now,)
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT box, COUNT(*) AS n FROM cards GROUP BY box"
            ).fetchall()
        boxes = {str(clamp_box(r["box"])): r["n"] for r in rows}
        return {"due_count": int(due), "boxes": boxes}

    def dashboard(self) -> dict[str, Any]:
        """Aggregate stats for the practice dashboard (all read-only).

        Returns card mastery distribution, 14-day activity, streak and the
        weakest questions (>= 2 attempts, lowest average score).
        """
        import time as _time
        from datetime import date as _date
        from datetime import timedelta as _timedelta

        now = _time.time()
        with self._lock:
            conn = self._ensure_db()
            cards_total = conn.execute(
                "SELECT COUNT(*) AS n FROM cards"
            ).fetchone()["n"]
            mastered = conn.execute(
                "SELECT COUNT(*) AS n FROM cards WHERE box >= 4"
            ).fetchone()["n"]
            box_rows = conn.execute(
                "SELECT box, COUNT(*) AS n FROM cards GROUP BY box"
            ).fetchall()
            due = conn.execute(
                "SELECT COUNT(*) AS n FROM cards WHERE due_at <= ?", (now,)
            ).fetchone()["n"]
            att = conn.execute(
                "SELECT COUNT(*) AS n, AVG(is_correct) AS rate FROM attempts"
            ).fetchone()
            day_rows = conn.execute(
                """
                SELECT date(answered_at, 'unixepoch', 'localtime') AS day,
                       COUNT(*) AS n, SUM(is_correct) AS correct
                FROM attempts GROUP BY day ORDER BY day
                """
            ).fetchall()
            all_days = conn.execute(
                """
                SELECT DISTINCT date(answered_at, 'unixepoch', 'localtime') AS d
                FROM attempts ORDER BY d
                """
            ).fetchall()
            weak = conn.execute(
                """
                SELECT q.question_id, q.question, q.category, q.type,
                       COUNT(*) AS attempts, AVG(a.score) AS avg_score
                FROM attempts a JOIN questions q ON q.question_id = a.question_id
                GROUP BY a.question_id
                HAVING COUNT(*) >= 2
                ORDER BY avg_score ASC, attempts DESC
                LIMIT 5
                """
            ).fetchall()

        boxes = {str(clamp_box(r["box"])): r["n"] for r in box_rows}
        attempts_total = int(att["n"])
        correct_rate = round(float(att["rate"]) * 100) if attempts_total else 0

        # 14-day series, ascending, zero-filled for days without attempts.
        by_day = {r["day"]: (r["n"], r["correct"]) for r in day_rows}
        today = _date.today()
        series = []
        for i in range(13, -1, -1):
            d = (today - _timedelta(days=i)).isoformat()
            n, correct = by_day.get(d, (0, 0))
            series.append({"day": d, "count": int(n), "correct": int(correct or 0)})

        # Streak: consecutive practiced days ending today (or yesterday when
        # today has no attempt yet — the streak shouldn't vanish before midnight).
        day_set = {r["d"] for r in all_days}
        streak = 0
        cursor = today
        if cursor.isoformat() not in day_set:
            cursor -= _timedelta(days=1)
        while cursor.isoformat() in day_set:
            streak += 1
            cursor -= _timedelta(days=1)

        return {
            "cards_total": int(cards_total),
            "mastered": int(mastered),
            "due_count": int(due),
            "boxes": boxes,
            "attempts_total": attempts_total,
            "correct_rate": correct_rate,
            "streak_days": streak,
            "last_14_days": series,
            "weak_spots": [
                {
                    "question_id": r["question_id"],
                    "question": r["question"],
                    "category": r["category"] or "",
                    "type": r["type"],
                    "attempts": int(r["attempts"]),
                    "avg_score": round(float(r["avg_score"]), 2),
                }
                for r in weak
            ],
        }


def _default_db_path() -> Path:
    from agent_assistant.config import settings

    return settings.practice_db_path


class _LazyStore:
    """Defer db path resolution until first use (tests can swap instance)."""

    def __init__(self) -> None:
        self._store: PracticeStore | None = None

    def _get(self) -> PracticeStore:
        if self._store is None:
            self._store = PracticeStore(_default_db_path())
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


# Singleton (lazy)
practice_store: Any = _LazyStore()
