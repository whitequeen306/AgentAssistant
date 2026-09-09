"""Episodic memory — Chroma vector store for important events.

Events are stored with metadata (timestamp, importance, source).
Retrieved on-demand via Agentic RAG (model decides when to search).

Chunking: semantic 512 tokens + 64 overlap (MVP).
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Chunking parameters
CHUNK_SIZE = 512  # tokens per chunk
CHUNK_OVERLAP = 64  # overlap between chunks


class EpisodicStore:
    """Chroma-backed episodic memory store.

    Stores important conversation events for later retrieval.
    Uses Chroma's default embedding (all-MiniLM-L6-v2) for MVP.
    """

    def __init__(self, persist_dir: Path, collection_name: str = "episodic") -> None:
        self._persist_dir = persist_dir
        self._collection_name = collection_name
        self._client = None
        self._collection = None

    def _ensure_collection(self):
        """Lazy-init Chroma client and collection."""
        if self._collection is not None:
            return

        import chromadb

        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        logger.debug(
            "Episodic store ready: %s (%d items)",
            self._collection_name,
            self._collection.count(),
        )

    def add_event(
        self,
        content: str,
        *,
        importance: float = 0.5,
        source: str = "conversation",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store an episodic event.

        Args:
            content: The event text to store.
            importance: 0.0-1.0 importance score (model-judged).
            source: Where this event came from.
            metadata: Additional metadata.

        Returns:
            The event ID.
        """
        self._ensure_collection()

        event_id = str(uuid.uuid4())
        meta = {
            "importance": importance,
            "source": source,
            "timestamp": time.time(),
            **(metadata or {}),
        }

        self._collection.add(
            ids=[event_id],
            documents=[content],
            metadatas=[meta],
        )
        logger.debug("Event stored: %s (importance=%.2f)", event_id[:8], importance)
        return event_id

    def add_chunked(
        self,
        text: str,
        *,
        importance: float = 0.5,
        source: str = "document",
        metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        """Store a long text as multiple chunks with overlap.

        Uses simple token-based chunking (MVP).
        Returns list of chunk IDs.
        """

        chunks = self._chunk_text(text)
        ids = []
        for i, chunk in enumerate(chunks):
            chunk_meta = {**(metadata or {}), "chunk_index": i, "total_chunks": len(chunks)}
            eid = self.add_event(
                chunk, importance=importance, source=source, metadata=chunk_meta
            )
            ids.append(eid)
        return ids

    def search(
        self,
        query: str,
        *,
        n_results: int = 5,
        min_importance: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Search episodic memory by semantic similarity.

        Returns list of dicts with: id, content, importance, score, metadata.
        """
        self._ensure_collection()

        if self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(n_results, self._collection.count()),
        )

        events = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            importance = meta.get("importance", 0.5)
            if importance < min_importance:
                continue

            # Chroma cosine distance → similarity score
            distance = results["distances"][0][i] if results["distances"] else 0.0
            score = 1.0 - distance  # cosine similarity

            events.append({
                "id": results["ids"][0][i],
                "content": doc,
                "importance": importance,
                "score": score,
                "metadata": meta,
            })

        return events

    def count(self) -> int:
        """Number of stored events."""
        self._ensure_collection()
        return self._collection.count()

    def delete_by_metadata(self, key: str, value: str) -> int:
        """Delete all documents whose metadata[key] equals value. Returns count."""
        self._ensure_collection()
        if not key or value is None:
            return 0
        try:
            found = self._collection.get(where={key: value}, include=[])
        except Exception as e:
            logger.warning("delete_by_metadata get failed: %s", e)
            return 0
        ids = found.get("ids") or []
        if not ids:
            return 0
        self._collection.delete(ids=ids)
        return len(ids)

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks by approximate token count.

        MVP: uses character-based approximation (4 chars ≈ 1 token).
        """
        # Approximate: 4 chars per token for mixed content
        chars_per_chunk = CHUNK_SIZE * 4
        chars_overlap = CHUNK_OVERLAP * 4

        chunks = []
        start = 0
        while start < len(text):
            end = start + chars_per_chunk
            chunk = text[start:end]
            if chunk.strip():
                chunks.append(chunk.strip())
            start = end - chars_overlap

        return chunks if chunks else [text]
