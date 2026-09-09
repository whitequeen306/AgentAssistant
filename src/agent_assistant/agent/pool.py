"""Per-conversation AgentLoop pool.

Each conversation owns an isolated AgentLoop (live ``_messages``, tool-call
pairing, short-term MemoryManager). Switching conversations no longer reloads
history into a shared loop — that design let concurrent turns (e.g. perf
report vs DeepResearch) corrupt tool_calls pairing and bleed context.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from agent_assistant.agent.loop import AgentEvent, AgentLoop

logger = logging.getLogger(__name__)


class AgentPool:
    """Lazy map of ``conversation_id → AgentLoop``."""

    def __init__(
        self,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._loops: dict[str, AgentLoop] = {}
        self._lock = threading.RLock()

    def __contains__(self, conv_id: str) -> bool:
        with self._lock:
            return conv_id in self._loops

    def get(self, conv_id: str) -> AgentLoop | None:
        with self._lock:
            return self._loops.get(conv_id)

    def ensure(
        self,
        conv_id: str,
        stored_messages: list[dict[str, Any]] | None = None,
    ) -> AgentLoop:
        """Return the loop for ``conv_id``, creating + hydrating if needed.

        If the loop already exists, ``stored_messages`` is ignored — the live
        in-memory tool trace must not be wiped by a UI switch/reload.
        """
        with self._lock:
            existing = self._loops.get(conv_id)
            if existing is not None:
                return existing
            loop = AgentLoop(
                on_event=self._on_event,
                conversation_id=conv_id,
            )
            if stored_messages:
                loop.load_history(stored_messages)
            self._loops[conv_id] = loop
            logger.debug(
                "AgentPool: created loop for conv=%s (hydrated=%d msgs)",
                conv_id[:8],
                len(stored_messages or []),
            )
            return loop

    def chat(
        self,
        conv_id: str,
        text: str,
        *,
        turn_id: str | None = None,
    ) -> str:
        """Run one user turn on the conversation's isolated loop.

        On first use, hydrates from the UI store (excluding the trailing user
        message equal to ``text``, because ``AgentLoop.chat`` will append it).
        """
        with self._lock:
            loop = self._loops.get(conv_id)
            if loop is None:
                loop = AgentLoop(
                    on_event=self._on_event,
                    conversation_id=conv_id,
                )
                try:
                    from agent_assistant.ui.store import ui_store

                    stored = ui_store.list_messages(conv_id)
                except Exception:
                    stored = []
                # send_message / report_perf persist the user turn before chat();
                # drop that trailing duplicate so chat() can append it once.
                if (
                    stored
                    and stored[-1].get("role") == "user"
                    and (stored[-1].get("content") or "") == text
                ):
                    stored = stored[:-1]
                if stored:
                    loop.load_history(stored)
                # Publish only after first-use hydration is complete. A second
                # thread for this conversation waits on _lock and receives this
                # fully initialized loop.
                self._loops[conv_id] = loop

        if turn_id is None:
            return loop.chat(text)
        return loop.chat(text, turn_id=turn_id)

    def discard(self, conv_id: str) -> None:
        """Drop a conversation's loop (e.g. after delete)."""
        with self._lock:
            removed = self._loops.pop(conv_id, None)
        if removed is not None:
            logger.debug("AgentPool: discarded loop for conv=%s", conv_id[:8])

    def request_cancel(self, conv_id: str | None = None) -> bool:
        """Cancel an in-flight turn. If conv_id is None, cancel all loops."""
        with self._lock:
            if conv_id is None:
                loops = list(self._loops.values())
            else:
                loop = self._loops.get(conv_id)
                if loop is None:
                    return False
                loops = [loop]
        for loop in loops:
            loop.request_cancel()
        return bool(loops)

    def active_count(self) -> int:
        with self._lock:
            return len(self._loops)
