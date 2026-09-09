"""A4 — Layered memory system.

Components:
- token_counter: tiktoken-based token counting
- manager: short-term window + rolling summary + compaction
- profile: stable user profile (SQLite, injected each round)
- episodic: Chroma vector store for episodic events
- retrieval: hybrid retrieval (dense + BM25) + rerank
"""
