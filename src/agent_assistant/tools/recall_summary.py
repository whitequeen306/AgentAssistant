"""recall_summary —— 取回被归档的历史摘要层。

滚动摘要采用拼接策略（旧层不重写）。层数累积到上限时，最老的层被移入
``<data_dir>/summaries/``，上下文里只留一行指针。这个工具就是那条指针的
落点——没有它，归档等于"清掉即永久丢失"。
"""

from __future__ import annotations

from typing import Any

from agent_assistant.memory import summary_archive
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult


class RecallSummaryTool(Tool):
    """Fetch archived rolling-summary layers (and search across them)."""

    @property
    def name(self) -> str:
        return "recall_summary"

    @property
    def description(self) -> str:
        return (
            "Retrieve an EARLIER summary layer of this conversation that was "
            "archived to disk once the rolling summary hit its cap — use it "
            "when you see a pointer such as 「更早的摘要已归档：<file>.md」 and "
            "need detail from before that point. Modes: pass `name` to read one "
            "archived layer; pass `keyword` to search all of them; pass nothing "
            "to list what is archived. Long layers are paginated (continue with "
            "`offset`). This is a relatively expensive call — use it only when "
            "the details are actually needed."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="name",
                type="string",
                required=False,
                description=(
                    "Exact archived file name to read (from a pointer line or "
                    "from the no-argument listing)."
                ),
            ),
            ToolParameter(
                name="keyword",
                type="string",
                required=False,
                description=(
                    "Case-insensitive keyword searched across all archived "
                    "summary layers."
                ),
            ),
            ToolParameter(
                name="offset",
                type="integer",
                required=False,
                description="Char offset into the layer text — continue a truncated read.",
            ),
            ToolParameter(
                name="limit",
                type="integer",
                required=False,
                description="Max characters to return (default 4000, max 20000).",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        name = str(kwargs.get("name") or "").strip()
        keyword = str(kwargs.get("keyword") or "").strip()
        offset = max(0, int(kwargs.get("offset") or 0))
        limit = min(20_000, max(500, int(kwargs.get("limit") or 4000)))

        if name:
            text = summary_archive.read_summary(name, offset=offset, limit=limit)
            if not text:
                _trace("read", False, f"miss name={name}")
                return ToolResult.failure(
                    f"no archived summary named '{name}'. Call recall_summary "
                    "with no arguments to list the archived layers.",
                    code=404,
                    error_category="not_found",
                )
            truncated = len(text) >= limit
            data: dict[str, Any] = {
                "name": name,
                "offset": offset,
                "text": text,
                "truncated": truncated,
            }
            if truncated:
                data["next_offset"] = offset + len(text)
            _trace("read", True, f"name={name} {len(text)} chars")
            return ToolResult.success(data)

        if keyword:
            hits = summary_archive.search_summaries(keyword, limit=10)
            _trace("search", bool(hits), f"kw={keyword} → {len(hits)} hit(s)")
            return ToolResult.success({
                "keyword": keyword,
                "results": hits,
                "note": "" if hits else "没有归档摘要命中该关键词",
            })

        _trace("list", True, "list archived layers")
        return ToolResult.success({
            "archived": summary_archive.list_summaries(limit=50),
            "note": "传 name 读取某一层，或传 keyword 检索。",
        })


def _trace(mode: str, ok: bool, detail: str) -> None:
    from agent_assistant.memory.session_trace import trace_recall

    trace_recall(mode, ok, detail, tool="recall_summary")


__all__ = ["RecallSummaryTool"]
