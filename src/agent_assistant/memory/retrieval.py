"""Hybrid retrieval — dense (Chroma) + sparse (BM25) + LLM rerank.

Implements the full retrieval pipeline:
1. Dense retrieval via Chroma semantic search
2. Sparse retrieval via BM25 keyword matching
3. Reciprocal Rank Fusion (RRF) to merge results
4. LLM-based rerank for final ordering
5. Query rewriting (HyDE) for ambiguous queries
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_assistant.memory.episodic import EpisodicStore

logger = logging.getLogger(__name__)

# RRF constant (standard value from the paper)
_RRF_K = 60


class HybridRetriever:
    """Combines dense + sparse retrieval with reranking.

    Works with any EpisodicStore-shaped collection (episodic memory or
    knowledge base). Pass ``store=``; ``episodic=`` remains as an alias.
    """

    def __init__(
        self,
        store: EpisodicStore | None = None,
        *,
        episodic: EpisodicStore | None = None,
        reranker: Callable[[str, list[dict]], list[dict]] | None = None,
        query_rewriter: Callable[[str], str] | None = None,
    ) -> None:
        resolved = store if store is not None else episodic
        if resolved is None:
            raise TypeError("HybridRetriever requires store= or episodic=")
        self._store = resolved
        self._reranker = reranker or _default_reranker
        self._query_rewriter = query_rewriter or _default_query_rewriter

    def retrieve(
        self,
        query: str,
        *,
        n_results: int = 5,
        use_rerank: bool = True,
        use_rewrite: bool = False,
    ) -> list[dict[str, Any]]:
        """Full retrieval pipeline.

        Args:
            query: User's natural language query.
            n_results: Final number of results to return.
            use_rerank: Whether to apply LLM reranking.
            use_rewrite: Whether to rewrite query (HyDE) first.

        Returns:
            List of event dicts sorted by relevance.
        """
        # Step 1: Optionally rewrite query
        search_query = query
        if use_rewrite:
            try:
                search_query = self._query_rewriter(query)
                logger.debug("Query rewritten: %s → %s", query[:50], search_query[:50])
            except Exception as e:
                logger.warning("Query rewrite failed, using original: %s", e)

        # Step 2: Dense retrieval (Chroma)
        dense_results = self._store.search(search_query, n_results=n_results * 2)

        # Step 3: Sparse retrieval (BM25)
        sparse_results = self._bm25_search(search_query, n_results=n_results * 2)

        # Step 4: Reciprocal Rank Fusion
        fused = self._rrf_merge(dense_results, sparse_results)

        # Step 5: Rerank
        if use_rerank and len(fused) > n_results:
            try:
                fused = self._reranker(query, fused[:n_results * 2])
            except Exception as e:
                logger.warning("Rerank failed, using RRF order: %s", e)

        return fused[:n_results]

    def _bm25_search(self, query: str, n_results: int = 10) -> list[dict[str, Any]]:
        """BM25 keyword search over all documents in the store."""
        self._store._ensure_collection()
        collection = self._store._collection

        if collection.count() == 0:
            return []

        # Get all documents (for BM25 corpus)
        all_data = collection.get(include=["documents", "metadatas"])
        if not all_data["documents"]:
            return []

        docs = all_data["documents"]
        ids = all_data["ids"]
        metas = all_data["metadatas"] or [{}] * len(docs)

        # Tokenize for BM25
        try:
            from rank_bm25 import BM25Okapi

            tokenized_corpus = [doc.lower().split() for doc in docs]
            bm25 = BM25Okapi(tokenized_corpus)
            query_tokens = query.lower().split()
            scores = bm25.get_scores(query_tokens)

            # Sort by BM25 score
            ranked_indices = sorted(
                range(len(scores)), key=lambda i: scores[i], reverse=True
            )[:n_results]

            results = []
            for idx in ranked_indices:
                if scores[idx] <= 0:
                    break
                results.append({
                    "id": ids[idx],
                    "content": docs[idx],
                    "importance": metas[idx].get("importance", 0.5),
                    "score": float(scores[idx]),
                    "metadata": metas[idx],
                })
            return results

        except ImportError:
            logger.warning("rank_bm25 not installed, skipping sparse retrieval")
            return []

    def _rrf_merge(
        self,
        dense: list[dict[str, Any]],
        sparse: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion to merge two ranked lists."""
        scores: dict[str, float] = {}
        doc_map: dict[str, dict[str, Any]] = {}

        # Dense results
        for rank, doc in enumerate(dense):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            doc_map[doc_id] = doc

        # Sparse results
        for rank, doc in enumerate(sparse):
            doc_id = doc["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
            doc_map[doc_id] = doc

        # Sort by fused score
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        results = []
        for doc_id in sorted_ids:
            entry = doc_map[doc_id].copy()
            entry["rrf_score"] = scores[doc_id]
            results.append(entry)

        return results


def _default_reranker(query: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM-based reranking: ask model to order by relevance."""
    from agent_assistant.llm.client import llm_client

    if len(docs) <= 1:
        return docs

    # Build rerank prompt
    doc_list = "\n".join(
        f"[{i}] {doc['content'][:200]}" for i, doc in enumerate(docs)
    )
    prompt = (
        f"Query: {query}\n\n"
        f"Documents:\n{doc_list}\n\n"
        "Rank these documents by relevance to the query. "
        "Return ONLY a comma-separated list of indices, most relevant first. "
        "Example: 2,0,4,1,3"
    )

    response = llm_client.chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    text = response.choices[0].message.content or ""

    # Parse indices
    try:
        indices = [int(x.strip()) for x in text.split(",") if x.strip().isdigit()]
        ranked = [docs[i] for i in indices if i < len(docs)]
        # Append any missing docs at the end
        seen = {i for i in indices if i < len(docs)}
        ranked.extend(docs[i] for i in range(len(docs)) if i not in seen)
        return ranked
    except (ValueError, IndexError):
        logger.warning("Failed to parse rerank response: %s", text[:100])
        return docs


def _default_query_rewriter(query: str) -> str:
    """HyDE-style query expansion: generate a hypothetical answer."""
    from agent_assistant.llm.client import llm_client

    response = llm_client.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "Rewrite the user's query into a more specific search query "
                    "that would retrieve relevant memories. Expand abbreviations, "
                    "add context. Output only the rewritten query."
                ),
            },
            {"role": "user", "content": query},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content or query
