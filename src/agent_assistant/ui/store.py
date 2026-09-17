"""SQLite persistence for the multi-page UI (docs/05 §5.2/5.4/5.5).

Tables:
- conversations(id, title, pinned, created_at)  — sidebar history
- messages(id, conversation_id, role, content, created_at)
- scenes(id, name, trigger_phrases JSON, actions JSON, created_at)
- settings(key, value)  — UI settings, applied at runtime without restart

Thread-safe: bridge calls arrive from the webview thread and background
worker threads, so every access goes through a single lock.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Settings defaults (§5.5) — everything the Settings page can edit
DEFAULT_SETTINGS: dict[str, str] = {
    "theme": "system",            # light / dark / system
    "accent": "#007AFF",
    "startup_state": "main",  # main / docked-search
    "minimize_behavior": "box",    # box / tray
    "edge_snap": "on",             # on / off
    # Feature toggles (§5.5 功能开关)
    "feature_right_click": "on",
    # 学习画像（SystemPrompt「背景参考」注入；练习室自定义出题也读取）
    "profile_major": "",
    "profile_grade": "",
    "profile_goal": "",
    "profile_note": "",
    # Thinking intensity for DeepSeek (off disables the CoT stream)
    "reasoning_effort": "low",
}


class UIStore:
    """SQLite-backed store for conversations, scenes and UI settings."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _ensure_db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                    ON messages(conversation_id);
                CREATE TABLE IF NOT EXISTS scenes (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    trigger_phrases TEXT NOT NULL,
                    actions TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()
        return self._conn

    # ─── Conversations ─────────────────────────────────────────────

    def create_conversation(self, title: str = "") -> dict[str, Any]:
        conv_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO conversations (id, title, pinned, created_at) "
                "VALUES (?, ?, 0, ?)",
                (conv_id, title, datetime.now().isoformat()),
            )
            conn.commit()
        return {"id": conv_id, "title": title, "pinned": False}

    def list_conversations(self, query: str = "") -> list[dict[str, Any]]:
        """Pinned first, then newest first. Optional title filter."""
        with self._lock:
            conn = self._ensure_db()
            if query:
                rows = conn.execute(
                    "SELECT * FROM conversations WHERE title LIKE ? "
                    "ORDER BY pinned DESC, created_at DESC",
                    (f"%{query}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations "
                    "ORDER BY pinned DESC, created_at DESC"
                ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "pinned": bool(r["pinned"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def find_or_create_conversation(self, title: str) -> dict[str, Any]:
        """Return the conversation with this EXACT title, creating it if absent.

        Used to route background reports (perf, briefing) to a dedicated
        conversation. Exact-match only — list_conversations' LIKE would also
        match a partial like "性能检测" inside "性能检测报告".
        """
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM conversations WHERE title = ? "
                "ORDER BY pinned DESC, created_at DESC LIMIT 1",
                (title,),
            ).fetchone()
        if row:
            return {
                "id": row["id"],
                "title": row["title"],
                "pinned": bool(row["pinned"]),
                "created_at": row["created_at"],
            }
        return self.create_conversation(title)

    def rename_conversation(self, conv_id: str, title: str) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id)
            )
            conn.commit()

    def set_title_if_empty(self, conv_id: str, title: str) -> None:
        """Title = first user message, truncated (§5.2)."""
        title = title.strip().replace("\n", " ")
        if len(title) > 40:
            title = title[:40] + "…"
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "UPDATE conversations SET title = ? "
                "WHERE id = ? AND (title = '' OR title IS NULL)",
                (title, conv_id),
            )
            conn.commit()

    def set_pinned(self, conv_id: str, pinned: bool) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "UPDATE conversations SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, conv_id),
            )
            conn.commit()

    def delete_conversation(self, conv_id: str) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()

    def purge_retired_report_conversations(self) -> int:
        """Delete conversations left by the removed briefing/perf features.

        Idempotent startup cleanup: 「晨间播报」/「性能检测」专用会话随功能
        一起移除，残留的旧记录（含消息）不再出现在会话列表里。
        """
        titles = ("晨间播报", "性能检测")
        removed = 0
        with self._lock:
            conn = self._ensure_db()
            placeholders = ",".join("?" * len(titles))
            rows = conn.execute(
                f"SELECT id FROM conversations WHERE title IN ({placeholders})",
                titles,
            ).fetchall()
            for row in rows:
                conn.execute(
                    "DELETE FROM messages WHERE conversation_id = ?", (row["id"],)
                )
                conn.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
                removed += 1
            conn.commit()
        return removed

    # ─── Messages ──────────────────────────────────────────────────

    def add_message(
        self, conv_id: str, role: str, content: str, msg_id: str | None = None
    ) -> str:
        """Insert a message; returns its id (S3: id is the dedup key)."""
        msg_id = msg_id or uuid.uuid4().hex
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(id, conversation_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg_id, conv_id, role, content, datetime.now().isoformat()),
            )
            conn.commit()
        return msg_id

    def list_messages(self, conv_id: str) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? "
                "ORDER BY created_at ASC",
                (conv_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ─── Scenes (§5.4) ─────────────────────────────────────────────

    def save_scene(self, scene: dict[str, Any]) -> dict[str, Any]:
        """Insert or update a scene. Returns the stored scene."""
        scene_id = scene.get("id") or f"scene-{uuid.uuid4().hex[:8]}"
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT INTO scenes (id, name, trigger_phrases, actions, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name = excluded.name, "
                "trigger_phrases = excluded.trigger_phrases, "
                "actions = excluded.actions",
                (
                    scene_id,
                    scene.get("name", ""),
                    json.dumps(scene.get("trigger_phrases", []), ensure_ascii=False),
                    json.dumps(scene.get("actions", []), ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        return self.get_scene(scene_id) or {}

    def get_scene(self, scene_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT * FROM scenes WHERE id = ?", (scene_id,)
            ).fetchone()
        return self._scene_row_to_dict(row) if row else None

    def list_scenes(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute(
                "SELECT * FROM scenes ORDER BY created_at ASC"
            ).fetchall()
        return [self._scene_row_to_dict(r) for r in rows]

    def delete_scene(self, scene_id: str) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute("DELETE FROM scenes WHERE id = ?", (scene_id,))
            conn.commit()

    def match_scene(self, text: str) -> dict[str, Any] | None:
        """Return the first scene whose trigger phrase appears in `text`.

        §5.4: the framework only detects the trigger — execution is
        injected to the agent, never hardcoded.
        """
        text_lower = text.strip().lower()
        if not text_lower:
            return None
        for scene in self.list_scenes():
            for phrase in scene.get("trigger_phrases", []):
                if phrase and phrase.lower() in text_lower:
                    return scene
        return None

    @staticmethod
    def _scene_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            phrases = json.loads(row["trigger_phrases"])
        except (json.JSONDecodeError, TypeError):
            phrases = []
        try:
            actions = json.loads(row["actions"])
        except (json.JSONDecodeError, TypeError):
            actions = []
        return {
            "id": row["id"],
            "name": row["name"],
            "trigger_phrases": phrases,
            "actions": actions,
            "created_at": row["created_at"],
        }

    # ─── Settings (§5.5) ───────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            conn = self._ensure_db()
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row:
            return row["value"]
        return DEFAULT_SETTINGS.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            conn = self._ensure_db()
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()

    def all_settings(self) -> dict[str, str]:
        """Defaults overlaid with stored values."""
        with self._lock:
            conn = self._ensure_db()
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        merged = dict(DEFAULT_SETTINGS)
        merged.update({r["key"]: r["value"] for r in rows})
        return merged

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _default_db_path() -> Path:
    from agent_assistant.config import settings
    return settings.data_dir / "ui.db"


class _LazyStore:
    """Defer db path resolution until first use (tests can swap instance)."""

    def __init__(self) -> None:
        self._store: UIStore | None = None

    def _get(self) -> UIStore:
        if self._store is None:
            self._store = UIStore(_default_db_path())
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


# Singleton (lazy)
ui_store: Any = _LazyStore()
