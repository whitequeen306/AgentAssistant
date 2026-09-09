"""Knowledge-base service — ingest/list/delete + hybrid retrieve."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from agent_assistant.config import settings
from agent_assistant.knowledge.ingest import clean_text, safe_filename, validate_source_path
from agent_assistant.knowledge.registry import KnowledgeFile, KnowledgeRegistry
from agent_assistant.memory.episodic import EpisodicStore
from agent_assistant.memory.retrieval import HybridRetriever

logger = logging.getLogger(__name__)


class KnowledgeService:
    """User-uploaded documents indexed in a dedicated Chroma collection."""

    def __init__(self) -> None:
        self._store: EpisodicStore | None = None
        self._retriever: HybridRetriever | None = None
        self._registry: KnowledgeRegistry | None = None
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        root = settings.knowledge_dir
        root.mkdir(parents=True, exist_ok=True)
        (root / "files").mkdir(parents=True, exist_ok=True)

        self._registry = KnowledgeRegistry(root / "files.json")
        self._store = EpisodicStore(
            persist_dir=root / "chroma",
            collection_name="knowledge",
        )
        self._retriever = HybridRetriever(store=self._store)
        self._initialized = True
        logger.info("Knowledge service initialized (dir=%s)", root)

    def _ensure(self) -> None:
        if not self._initialized:
            self.initialize()

    @property
    def registry(self) -> KnowledgeRegistry:
        self._ensure()
        assert self._registry is not None
        return self._registry

    @property
    def store(self) -> EpisodicStore:
        self._ensure()
        assert self._store is not None
        return self._store

    @property
    def retriever(self) -> HybridRetriever:
        self._ensure()
        assert self._retriever is not None
        return self._retriever

    def list_files(self) -> list[dict[str, Any]]:
        return [f.to_dict() for f in self.registry.list_files()]

    def ingest_path(self, path: str | Path, *, title: str = "") -> dict[str, Any]:
        """Clean → chunk → vectorize one local file. Isolated by file_id."""
        self._ensure()
        src = Path(path)
        ok, err = validate_source_path(src)
        if not ok:
            return {"ok": False, "error": err}

        try:
            raw = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return {"ok": False, "error": f"read failed: {e}"}

        text = clean_text(raw)
        if not text:
            return {"ok": False, "error": "no text after cleaning"}

        file_id = KnowledgeRegistry.new_id()
        filename = src.name
        display = (title or Path(filename).stem).strip() or filename
        stored_name = f"{file_id}_{safe_filename(filename)}"
        stored_path = settings.knowledge_dir / "files" / stored_name

        try:
            shutil.copy2(src, stored_path)
        except OSError as e:
            return {"ok": False, "error": f"copy failed: {e}"}

        try:
            chunk_ids = self.store.add_chunked(
                text,
                importance=0.7,
                source="knowledge",
                metadata={
                    "file_id": file_id,
                    "filename": filename,
                    "title": display,
                },
            )
        except Exception as e:
            stored_path.unlink(missing_ok=True)
            logger.exception("Knowledge ingest failed")
            return {"ok": False, "error": f"index failed: {e}"}

        entry = KnowledgeFile(
            file_id=file_id,
            title=display,
            filename=filename,
            stored_path=str(stored_path),
            size=stored_path.stat().st_size,
            chunk_count=len(chunk_ids),
        )
        self.registry.add(entry)
        logger.info(
            "Knowledge ingested: %s (%d chunks)",
            file_id,
            len(chunk_ids),
        )
        return {"ok": True, "file": entry.to_dict()}

    def delete_file(self, file_id: str) -> dict[str, Any]:
        """Remove registry entry, stored copy, and all vector chunks for file_id."""
        self._ensure()
        file_id = (file_id or "").strip()
        if not file_id:
            return {"ok": False, "error": "file_id required"}

        entry = self.registry.remove(file_id)
        if entry is None:
            return {"ok": False, "error": "file not found"}

        deleted = self.store.delete_by_metadata("file_id", file_id)
        try:
            Path(entry.stored_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Failed to delete stored file %s: %s", entry.stored_path, e)

        return {
            "ok": True,
            "file_id": file_id,
            "chunks_deleted": deleted,
        }

    def retrieve(self, query: str, *, n_results: int = 5) -> list[dict[str, Any]]:
        """Hybrid search + rerank over the knowledge collection."""
        self._ensure()
        query = (query or "").strip()
        if not query:
            return []
        return self.retriever.retrieve(query, n_results=n_results, use_rerank=True)


knowledge_service = KnowledgeService()
