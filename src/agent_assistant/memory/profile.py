"""Stable user profile — persistent facts injected every round.

Stored in SQLite (key-value pairs). The model updates the profile
when it learns stable facts about the user (name, preferences, etc.).

Cap: ~500 tokens. Injected into system prompt each round.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from agent_assistant.memory.token_counter import count_tokens

logger = logging.getLogger(__name__)


class ProfileStore:
    """SQLite-backed stable user profile.

    Stores key-value pairs representing stable user facts.
    The full profile is serialized to text and injected into
    the system prompt each conversation round.

    Thread-safe: check_same_thread=False + a lock (accessed from the agent
    loop thread and the init/UI thread).
    """

    def __init__(self, db_path: Path, token_cap: int = 500) -> None:
        self._db_path = db_path
        self._token_cap = token_cap
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _ensure_db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            self._conn.commit()
        return self._conn

    def get(self, key: str) -> str | None:
        """Get a single profile value."""
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT value FROM profile WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        """Set/update a profile entry."""
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT OR REPLACE INTO profile (key, value, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value),
            )
            conn.commit()
            logger.debug("Profile updated: %s = %s", key, value[:50])

    def delete(self, key: str) -> None:
        """Remove a profile entry."""
        with self._lock:
            conn = self._ensure_db()
            conn.execute("DELETE FROM profile WHERE key = ?", (key,))
            conn.commit()

    def all_entries(self) -> dict[str, str]:
        """Get all profile entries as a dict."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute("SELECT key, value FROM profile").fetchall()
            return {k: v for k, v in rows}

    def render(self) -> str:
        """Render profile as text for system prompt injection.

        Returns empty string if no entries or if over token cap
        (shouldn't happen with proper management).
        """
        entries = self.all_entries()
        if not entries:
            return ""

        lines = [f"- {k}: {v}" for k, v in entries.items()]
        text = "\n".join(lines)

        # Enforce token cap — drop the OLDEST entries if needed.
        #
        # ``lines`` is in insertion order (oldest first) and a profile holds
        # *stable facts*, so the thing that should go is the stale one: a
        # newly learned fact (new city, new goal) is worth more than the
        # first thing ever recorded. The previous implementation did the
        # opposite — it kept the oldest and dropped the newest, despite the
        # comment claiming otherwise — which is backwards for a user profile.
        #
        # At least one entry survives even if it alone exceeds the cap;
        # an empty profile section is worse than an over-cap one.
        if count_tokens(text) > self._token_cap:
            kept: list[str] = []
            used = 0
            for line in reversed(lines):
                cost = count_tokens(line)
                if kept and used + cost > self._token_cap:
                    break
                kept.insert(0, line)
                used += cost
            text = "\n".join(kept)

        return text

    def build_prompt_section(self) -> str:
        """Build the system prompt section for user profile."""
        rendered = self.render()
        if not rendered:
            return ""
        return f"\n\n## User Profile (stable facts)\n{rendered}\n"

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
