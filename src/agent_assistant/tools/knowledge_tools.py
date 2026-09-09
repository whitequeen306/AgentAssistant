"""Knowledge-base tools — hybrid RAG over user-uploaded documents."""

from __future__ import annotations

from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult


class SearchKnowledgeTool(Tool):
    """Search the user knowledge base (uploaded files, vector-indexed)."""

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "Search the user's knowledge base (files they uploaded and indexed). "
            "Uses hybrid retrieval (dense + keyword) and reranking, then returns "
            "text chunks with source filenames. "
            "When to call: the user enabled the knowledge base for this turn, "
            "or asks about content in their uploaded documents. "
            "When NOT to call: agent-saved notes in the library → use "
            "read_attached_source if attached; past conversations → recall_memory; "
            "general web facts → web_search."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="Natural-language query against the knowledge base",
            ),
            ToolParameter(
                name="n_results",
                type="number",
                description="Max chunks to return (default 5)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        query: str = (kwargs.get("query") or "").strip()
        n_results: int = int(kwargs.get("n_results") or 5)
        n_results = max(1, min(n_results, 12))

        if not query:
            return ToolResult.failure("parameter 'query' is required")

        from agent_assistant.knowledge.service import knowledge_service

        results = knowledge_service.retrieve(query, n_results=n_results)
        chunks = [
            {
                "content": r.get("content", "")[:1200],
                "score": round(
                    float(r.get("rrf_score", r.get("score", 0)) or 0),
                    3,
                ),
                "file_id": (r.get("metadata") or {}).get("file_id", ""),
                "filename": (r.get("metadata") or {}).get("filename", ""),
                "title": (r.get("metadata") or {}).get("title", ""),
            }
            for r in results
        ]
        return ToolResult.success(data={"chunks": chunks, "count": len(chunks)})


class ReadAttachedSourceTool(Tool):
    """Read a user-attached library note or local file for this turn."""

    @property
    def name(self) -> str:
        return "read_attached_source"

    @property
    def description(self) -> str:
        return (
            "Read a library note or local file the user attached for this turn. "
            "When to call: user attached 资料库 notes or 本机文件 and you need "
            "their content. When NOT to call: knowledge-base RAG → search_knowledge; "
            "unattached arbitrary paths → do not invent paths."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="Note filename or absolute file path from the attached list",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path = (kwargs.get("path") or "").strip()
        if not path:
            return ToolResult.failure("parameter 'path' is required")

        from agent_assistant.research.local_sources import (
            get_pending_local_sources,
            load_source_text,
        )

        allowed = {s.path: s for s in get_pending_local_sources()}
        for s in list(allowed.values()):
            allowed[s.path.replace("\\", "/").split("/")[-1]] = s

        source = allowed.get(path) or allowed.get(path.replace("\\", "/").split("/")[-1])
        if source is None:
            return ToolResult.failure("path is not in the attached sources list")

        ok, text = load_source_text(source)
        if not ok:
            return ToolResult.failure(text)
        return ToolResult.success(
            data={
                "path": source.path,
                "kind": source.kind,
                "title": source.title or source.path,
                "content": text,
            }
        )
