"""Memory tools — Agentic RAG interface for the model.

These tools let the model autonomously:
- recall_memory: search episodic memory when context is insufficient
- save_memory: store important events for future recall
- update_profile: update stable user profile facts

The model decides WHEN to call these (agentic principle).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


class RecallMemoryTool(Tool):
    """Search episodic memory (Agentic RAG).

    The model calls this when it determines the current context
    and user profile are insufficient to answer — it needs to
    recall past events/conversations.
    """

    @property
    def name(self) -> str:
        return "recall_memory"

    @property
    def description(self) -> str:
        return (
            "Search your long-term episodic memory for past events, "
            "conversations, or facts. Use when: the user references something "
            "from a previous conversation, asks 'do you remember...', or you "
            "need context not present in the current conversation. "
            "When NOT to call: the answer is in the current conversation or "
            "user profile — don't search unnecessarily."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="What to search for in memory (natural language)",
            ),
            ToolParameter(
                name="n_results",
                type="number",
                description="Max results to return (default 5)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        query: str = kwargs.get("query", "").strip()
        n_results: int = int(kwargs.get("n_results", 5))

        if not query:
            return ToolResult.failure("parameter 'query' is required")

        from agent_assistant.memory.service import memory_service

        retriever = memory_service.get_retriever()
        if retriever is None:
            return ToolResult.failure("Memory system not initialized")

        results = retriever.retrieve(query, n_results=n_results, use_rerank=True)

        if not results:
            return ToolResult.success(data={"memories": [], "count": 0})

        memories = [
            {
                "content": r["content"][:500],
                "score": round(r.get("rrf_score", r.get("score", 0)), 3),
                "importance": r.get("importance", 0.5),
                "source": r.get("metadata", {}).get("source", "unknown"),
            }
            for r in results
        ]

        return ToolResult.success(data={"memories": memories, "count": len(memories)})


class SaveMemoryTool(Tool):
    """Save an important event to long-term episodic memory.

    The model calls this when it judges something important enough
    to remember across conversations (decisions, preferences, facts).
    """

    @property
    def name(self) -> str:
        return "save_memory"

    @property
    def description(self) -> str:
        return (
            "Save an important event/fact to long-term memory for future recall. "
            "Use when: the user shares a preference, makes a decision, mentions "
            "an important event, or says 'remember this'. The model judges "
            "importance — not everything needs saving. "
            "When NOT to call: trivial chitchat, temporary context, things "
            "already in the user profile."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="content",
                type="string",
                description="The event/fact to remember (clear, self-contained text)",
            ),
            ToolParameter(
                name="importance",
                type="number",
                description="Importance 0.0-1.0 (0.5=default, 0.9+=critical)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        content: str = kwargs.get("content", "").strip()
        importance: float = float(kwargs.get("importance", 0.5))

        if not content:
            return ToolResult.failure("parameter 'content' is required")

        importance = max(0.0, min(1.0, importance))

        from agent_assistant.memory.service import memory_service

        store = memory_service.get_episodic_store()
        if store is None:
            return ToolResult.failure("Memory system not initialized")

        event_id = store.add_event(
            content, importance=importance, source="conversation"
        )

        return ToolResult.success(data={"saved": True, "event_id": event_id[:8]})


class UpdateProfileTool(Tool):
    """Update the stable user profile with a new fact.

    The model calls this when it learns a stable, persistent fact
    about the user (name, job, preferences, habits).
    """

    @property
    def name(self) -> str:
        return "update_profile"

    @property
    def description(self) -> str:
        return (
            "Update the user's stable profile with a persistent fact. "
            "Use when: user states their name, job, preferences, or any "
            "stable fact that should persist across all conversations. "
            "Use action='set' to add/update, 'delete' to remove. "
            "When NOT to call: temporary moods, one-time events "
            "(use save_memory for those)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="key",
                type="string",
                description="Profile field name (e.g. 'name', 'job', 'language_pref')",
            ),
            ToolParameter(
                name="value",
                type="string",
                description="The fact value (for action='set')",
                required=False,
            ),
            ToolParameter(
                name="action",
                type="string",
                description="'set' (default) or 'delete'",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        key: str = kwargs.get("key", "").strip()
        value: str = kwargs.get("value", "").strip()
        action: str = kwargs.get("action", "set").strip().lower()

        if not key:
            return ToolResult.failure("parameter 'key' is required")

        from agent_assistant.memory.service import memory_service

        profile = memory_service.get_profile_store()
        if profile is None:
            return ToolResult.failure("Memory system not initialized")

        if action == "delete":
            profile.delete(key)
            return ToolResult.success(data={"deleted": key})

        if not value:
            return ToolResult.failure("parameter 'value' is required for action='set'")

        profile.set(key, value)
        return ToolResult.success(data={"updated": key, "value": value})
