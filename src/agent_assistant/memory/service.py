"""Memory service — singleton that manages all memory components.

Provides unified access to:
- MemoryManager (short-term window + rolling summary)
- ProfileStore (stable user profile, SQLite)
- EpisodicStore (Chroma vector store)
- HybridRetriever (dense + BM25 + rerank)

Initialized lazily on first access.
"""

from __future__ import annotations

import logging

from agent_assistant.config import settings
from agent_assistant.memory.episodic import EpisodicStore
from agent_assistant.memory.manager import MemoryManager
from agent_assistant.memory.profile import ProfileStore
from agent_assistant.memory.retrieval import HybridRetriever

logger = logging.getLogger(__name__)


class MemoryService:
    """Central memory service managing all memory layers."""

    def __init__(self) -> None:
        self._memory_manager: MemoryManager | None = None
        self._profile_store: ProfileStore | None = None
        self._episodic_store: EpisodicStore | None = None
        self._retriever: HybridRetriever | None = None
        self._initialized = False

    def initialize(self) -> None:
        """Initialize all memory components (idempotent)."""
        if self._initialized:
            return

        data_dir = settings.data_dir
        memory_dir = data_dir / "memory"

        # Memory manager (short-term window)
        self._memory_manager = MemoryManager(
            token_budget=settings.memory_token_budget,
            summary_cap=settings.memory_summary_cap,
            soft_rounds=settings.memory_soft_rounds,
        )

        # Profile store (SQLite)
        self._profile_store = ProfileStore(
            db_path=memory_dir / "profile.db",
            token_cap=settings.memory_profile_cap,
        )

        # Episodic store (Chroma)
        self._episodic_store = EpisodicStore(
            persist_dir=memory_dir / "chroma",
        )

        # Hybrid retriever
        self._retriever = HybridRetriever(episodic=self._episodic_store)

        self._initialized = True
        logger.info("Memory service initialized (dir=%s)", memory_dir)

    def get_memory_manager(self) -> MemoryManager | None:
        """Get the short-term memory manager."""
        if not self._initialized:
            self.initialize()
        return self._memory_manager

    def get_profile_store(self) -> ProfileStore | None:
        """Get the stable profile store."""
        if not self._initialized:
            self.initialize()
        return self._profile_store

    def get_episodic_store(self) -> EpisodicStore | None:
        """Get the episodic memory store."""
        if not self._initialized:
            self.initialize()
        return self._episodic_store

    def get_retriever(self) -> HybridRetriever | None:
        """Get the hybrid retriever."""
        if not self._initialized:
            self.initialize()
        return self._retriever

    def build_profile_additions(self) -> str:
        """User-level profile text for the system prompt (shared across convs)."""
        profile = self.get_profile_store()
        if not profile:
            return ""
        return profile.build_prompt_section() or ""

    def build_system_prompt_additions(self) -> str:
        """Build text to append to system prompt (profile + global summary).

        Prefer per-conversation ``AgentLoop`` memory for rolling summaries;
        the global manager summary remains for single-loop / test callers.
        """
        parts = []
        profile = self.build_profile_additions()
        if profile:
            parts.append(profile)

        manager = self.get_memory_manager()
        if manager:
            prefix = manager.build_context_prefix()
            if prefix:
                parts.append(prefix)

        return "".join(parts)


# Global singleton
memory_service = MemoryService()
