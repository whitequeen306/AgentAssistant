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
            token_budget=settings.effective_memory_budget,
            summary_cap=settings.memory_summary_cap,
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

    def build_study_profile_section(self) -> str:
        """学习画像（设置页 → SystemPrompt「背景参考」）。

        Reads the ui settings table; empty/missing fields are skipped and an
        all-empty profile yields "" (no section injected). Deliberately terse
        (~100 tokens) and framed as BACKGROUND, not persona constraints.
        """
        try:
            from agent_assistant.ui.store import ui_store

            def get(key: str) -> str:
                return (ui_store.get_setting(key, "") or "").strip()

            major = get("profile_major")
            grade = get("profile_grade")
            goal = get("profile_goal")
            note = get("profile_note")
        except Exception:
            return ""

        lines: list[str] = []
        identity = " ".join(x for x in (grade, major) if x)
        if identity:
            lines.append(f"- 专业/年级：{identity}")
        if goal:
            lines.append(f"- 当前目标：{goal}")
        if note:
            lines.append(f"- 补充说明：{note}")
        # 目标轨道从「用户已启用的轨道」派生，不另设画像字段——单一数据源，
        # 避免「画像选了考研但没建轨道」这类不一致状态。
        try:
            from agent_assistant.goals.registry import get_spec
            from agent_assistant.goals.store import goal_store

            for t in goal_store.active_tracks():
                spec = get_spec(t.kind)
                if spec is not None:
                    lines.append(f"- 目标轨道：{spec.label}（{spec.prompt_hint}）")
                    break
        except Exception:
            pass
        if not lines:
            return ""
        return (
            "# 用户画像（背景参考，非人设约束）\n"
            + "\n".join(lines)
            + "\n交流时可默认用户具备其专业背景，举例与建议优先贴近其专业和目标场景"
            "（备考资料、求职方向、术语深度按此校准）。这是背景信息而不是话题边界——"
            "其他话题照常正常回应，也不要反复主动提及这份画像。\n"
        )

        return "".join(parts)


# Global singleton
memory_service = MemoryService()
