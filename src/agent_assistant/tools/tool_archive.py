"""Tool-call detail store — FULL arguments + result per conversation.

Compaction and snapshot stubbing shrink what the model sees, but the FULL
result of every main-loop tool call is persisted here, so the agent can
retrieve exact past outputs via ``recall_tool_result`` instead of losing
them forever (the sub-agent runtime already had this via ``raw.jsonl``;
this is the main-loop counterpart).

Storage moved to the unified session layout::

    <data_dir>/sessions/<conversation_id>/tool_details.jsonl

alongside ``events.jsonl`` (see ``memory/session_trace.py``). One line per
call::

    {"v":1, "id":"T-0007", "call_id": "...", "conversation_id": "...",
     "ts": "...", "tool": "...", "arguments": "{...}", "ok": true,
     "result": {...}}

Writes are fire-and-forget from the agent loop's perspective: ``record()``
never raises, a failed archive write must never break tool execution.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from agent_assistant.config import settings
from agent_assistant.memory.session_trace import SessionTrace, safe_session_name

logger = logging.getLogger(__name__)

# Files older than this are deleted on periodic cleanup.
_RETENTION_DAYS = 7
_CLEANUP_EVERY_S = 600
# Search/pagination defaults for the recall tool.
_SEARCH_PREVIEW_CHARS = 200
_ARGS_PREVIEW_CHARS = 120
_FETCH_PAGE_CHARS = 20_000

_DETAIL_FILENAME = "tool_details.jsonl"


def _safe_conv_name(conversation_id: str) -> str:
    """Backwards-compatible alias (session dirs use the same sanitizer)."""
    return safe_session_name(conversation_id)


class ToolArchive:
    """Read/write facade over the per-session ``tool_details.jsonl`` store.

    The write path is delegated to :class:`SessionTrace` so the rotation
    rules, id sequence and oversized-result spilling live in exactly one
    place. This class adds the recall-oriented read API on top.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        max_file_bytes: int | None = None,
        retention_days: int = _RETENTION_DAYS,
    ) -> None:
        self._base_dir = base_dir
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        self._trace = SessionTrace(
            base_dir=base_dir,
            **({"max_file_bytes": max_file_bytes} if max_file_bytes else {}),
        )

    @property
    def base_dir(self) -> Path:
        return self._base_dir or (settings.data_dir / "sessions")

    @property
    def _max_file_bytes(self) -> int:
        return self._trace._max_file_bytes  # noqa: SLF001 — single owner

    # ── write path ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        conversation_id: str,
        call_id: str,
        tool: str,
        arguments: str | dict[str, Any],
        result: dict[str, Any],
    ) -> str | None:
        """Append one call record; returns its detail id (None on failure).

        Never raises (logs on failure).
        """
        self._maybe_cleanup()
        return self._trace.tool_detail(
            conversation_id,
            call_id=call_id,
            tool=tool,
            arguments=arguments,
            ok=bool(result.get("ok", False)),
            result=result,
        )

    # ── read path ─────────────────────────────────────────────────────────

    def _iter_records(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._trace.iter_tool_details(conversation_id)

    def get(
        self, conversation_id: str, call_id: str
    ) -> dict[str, Any] | None:
        """Full record for one call id, or None."""
        for rec in self._iter_records(conversation_id):
            if rec.get("call_id") == call_id:
                return self._hydrate(conversation_id, rec)
        return None

    def search(
        self,
        conversation_id: str,
        *,
        tool: str | None = None,
        keyword: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Matching records, MOST RECENT first, capped at ``limit``.

        ``tool`` filters by exact tool name; ``keyword`` matches (case-
        insensitive) anywhere in the arguments or result payload. Both None
        → the most recent calls regardless of tool.
        """
        kw = (keyword or "").strip().lower()
        matches: list[dict[str, Any]] = []
        for rec in reversed(self._iter_records(conversation_id)):
            if tool and rec.get("tool") != tool:
                continue
            if kw:
                haystack = (
                    str(rec.get("arguments", ""))
                    + " "
                    + json.dumps(rec.get("result", {}), ensure_ascii=False)
                ).lower()
                if kw not in haystack:
                    continue
            matches.append(rec)
            if len(matches) >= max(1, limit):
                break
        return matches

    def _hydrate(self, conversation_id: str, rec: dict[str, Any]) -> dict[str, Any]:
        """Inline a spilled (oversized) result on read, transparently."""
        result = rec.get("result")
        if isinstance(result, dict) and result.get("_ref"):
            text = self._trace.read_detail_file(conversation_id, result["_ref"])
            if text is not None:
                try:
                    rec = {**rec, "result": json.loads(text)}
                except json.JSONDecodeError:
                    pass
        return rec

    # ── retention ─────────────────────────────────────────────────────────

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < _CLEANUP_EVERY_S:
            return
        self._last_cleanup = now
        try:
            cutoff = now - self._retention_days * 86400
            for path in self.base_dir.glob("*/*.jsonl"):
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort retention; never block the write path


# Global singleton (main loop + recall tool share it; tests monkeypatch
# ``agent_assistant.agent.loop.tool_archive`` or construct their own).
tool_archive = ToolArchive()


# ── recall_tool_result ────────────────────────────────────────────────────

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult  # noqa: E402


def _current_conversation_id() -> str:
    from agent_assistant.subagents.context import current_execution_context

    context = current_execution_context()
    return context.conversation_id if context is not None else "local"


def _preview(obj: Any, max_chars: int) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


class RecallToolResultTool(Tool):
    """Fetch full archived tool results that compaction/stubbing shrank."""

    @property
    def name(self) -> str:
        return "recall_tool_result"

    @property
    def description(self) -> str:
        return (
            "Retrieve the FULL result of a past tool call from this session's "
            "archive — use when an earlier result was stubbed as "
            "「历史快照…已存档」 or dropped by context compaction and you now "
            "need its details. Modes: pass call_id (from an archive pointer or "
            "a previous search) for the exact record; pass tool_name and/or "
            "keyword to search this session's archive (most recent first); "
            "pass nothing to list recent calls. Long results are paginated — "
            "continue with offset. Current session only."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="call_id",
                type="string",
                required=False,
                description=(
                    "Exact tool_call_id to fetch (e.g. from a "
                    "「已存档 · call_xx」 pointer). Takes priority over search."
                ),
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                required=False,
                description="Filter search by exact tool name (e.g. ui_inspect).",
            ),
            ToolParameter(
                name="keyword",
                type="string",
                required=False,
                description=(
                    "Case-insensitive keyword matched against the call's "
                    "arguments and result payload."
                ),
            ),
            ToolParameter(
                name="limit",
                type="integer",
                required=False,
                description="Max search hits to return (default 5, max 20).",
            ),
            ToolParameter(
                name="offset",
                type="integer",
                required=False,
                description=(
                    "Char offset into the fetched record's result JSON — "
                    "continue a truncated fetch."
                ),
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        conversation_id = _current_conversation_id()
        call_id = str(kwargs.get("call_id") or "").strip()
        tool_name = str(kwargs.get("tool_name") or "").strip()
        keyword = str(kwargs.get("keyword") or "").strip()
        offset = max(0, int(kwargs.get("offset") or 0))
        limit = min(20, max(1, int(kwargs.get("limit") or 5)))

        if call_id:
            rec = tool_archive.get(conversation_id, call_id)
            if rec is None:
                _trace_recall(
                    conversation_id,
                    mode="fetch",
                    ok=False,
                    detail=f"miss call_id={call_id}",
                )
                return ToolResult.failure(
                    f"no archived record for call_id '{call_id}' in this "
                    "session. Use tool_name/keyword search to locate it.",
                    code=404,
                    error_category="not_found",
                )
            _trace_recall(
                conversation_id,
                mode="fetch",
                ok=True,
                detail=f"call_id={call_id} tool={rec.get('tool')}",
            )
            return ToolResult.success(self._paginated(rec, offset))

        hits = tool_archive.search(
            conversation_id, tool=tool_name or None, keyword=keyword or None,
            limit=limit,
        )
        if not hits:
            _trace_recall(
                conversation_id,
                mode="search",
                ok=False,
                detail=f"no hits tool={tool_name or '-'} kw={keyword or '-'}",
            )
            return ToolResult.failure(
                "no matching archived calls in this session "
                "(archive covers this session only).",
                code=404,
                error_category="not_found",
            )
        _trace_recall(
            conversation_id,
            mode="search",
            ok=True,
            detail=f"{len(hits)} hit(s) tool={tool_name or '-'} kw={keyword or '-'}",
        )
        return ToolResult.success(
            {
                "mode": "search",
                "matches": [
                    {
                        "call_id": h.get("call_id"),
                        "id": h.get("id"),
                        "ts": h.get("ts"),
                        "tool": h.get("tool"),
                        "arguments": _preview(
                            h.get("arguments", ""), _ARGS_PREVIEW_CHARS
                        ),
                        "ok": h.get("ok"),
                        "result_preview": _preview(
                            h.get("result", {}), _SEARCH_PREVIEW_CHARS
                        ),
                    }
                    for h in hits
                ],
                "hint": (
                    "call recall_tool_result with one of these call_id values "
                    "to get the full result."
                ),
            }
        )

    @staticmethod
    def _paginated(rec: dict[str, Any], offset: int) -> dict[str, Any]:
        result_json = json.dumps(rec.get("result", {}), ensure_ascii=False)
        total = len(result_json)
        page = result_json[offset : offset + _FETCH_PAGE_CHARS]
        out: dict[str, Any] = {
            "mode": "fetch",
            "call_id": rec.get("call_id"),
            "id": rec.get("id"),
            "ts": rec.get("ts"),
            "tool": rec.get("tool"),
            "arguments": rec.get("arguments"),
            "result_offset": offset,
            "result_total_chars": total,
            "result": page,
        }
        if offset + len(page) < total:
            out["truncated"] = True
            out["hint"] = (
                f"result continues beyond this page — call again with "
                f"offset={offset + len(page)}."
            )
        return out


def _trace_recall(
    conversation_id: str, *, mode: str, ok: bool, detail: str
) -> None:
    """Audit trail: what the model actually pulled back from the archive."""
    _ = conversation_id  # resolved from the execution context inside
    from agent_assistant.memory.session_trace import trace_recall

    trace_recall(mode, ok, detail, tool="recall_tool_result")
