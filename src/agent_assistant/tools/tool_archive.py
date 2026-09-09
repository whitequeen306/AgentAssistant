"""Tool-call archive — full tool call + result persisted per conversation.

Compaction and snapshot stubbing shrink what the model sees, but the FULL
result of every main-loop tool call is appended to a JSONL file here, so the
agent can retrieve exact past outputs via ``recall_tool_result`` instead of
losing them forever (the sub-agent runtime already had this via raw.jsonl;
this is the main-loop counterpart).

Layout: ``<data_dir>/tool_archive/<conversation>.jsonl`` (active) — when the
active file passes ``max_file_bytes`` it is renamed with a timestamp suffix
and a fresh active file starts, so reads stay bounded. One line per call:

    {"v":1, "call_id": "...", "conversation_id": "...", "ts": "...",
     "tool": "...", "arguments": "{...}", "ok": true, "result": {...}}

Writes are fire-and-forget from the agent loop's perspective: ``record()``
never raises, a failed archive write must never break tool execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_assistant.config import settings

logger = logging.getLogger(__name__)

# Records written to the active file beyond this size rotate it aside.
_MAX_FILE_BYTES = 5 * 1024 * 1024
# Files older than this are deleted on periodic cleanup.
_RETENTION_DAYS = 7
_CLEANUP_EVERY_S = 600
# Search/pagination defaults for the recall tool.
_SEARCH_PREVIEW_CHARS = 200
_ARGS_PREVIEW_CHARS = 120
_FETCH_PAGE_CHARS = 20_000


def _safe_conv_name(conversation_id: str) -> str:
    """Filesystem-safe per-conversation prefix (collision-safe)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", conversation_id).strip("_")
    safe = safe[:80] or "conv"
    if safe != conversation_id:
        digest = hashlib.md5(conversation_id.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return safe


class ToolArchive:
    """Append-only per-conversation archive of tool calls + full results."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
        retention_days: int = _RETENTION_DAYS,
    ) -> None:
        self._base_dir = base_dir
        self._max_file_bytes = max_file_bytes
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    @property
    def base_dir(self) -> Path:
        return self._base_dir or (settings.data_dir / "tool_archive")

    # ── write path ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        conversation_id: str,
        call_id: str,
        tool: str,
        arguments: str | dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Append one call record. Never raises (logs on failure)."""
        try:
            self._record_locked(
                conversation_id=conversation_id,
                call_id=call_id,
                tool=tool,
                arguments=arguments,
                result=result,
            )
        except Exception:
            logger.warning(
                "tool archive write failed (tool=%s call_id=%s)",
                tool,
                call_id,
                exc_info=True,
            )

    def _record_locked(
        self,
        *,
        conversation_id: str,
        call_id: str,
        tool: str,
        arguments: str | dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            self._maybe_cleanup()
            conv_dir = self.base_dir
            conv_dir.mkdir(parents=True, exist_ok=True)
            active = conv_dir / f"{_safe_conv_name(conversation_id)}.jsonl"
            if active.exists() and active.stat().st_size >= self._max_file_bytes:
                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                # Always suffix -N: unique within the same second (Windows
                # rename refuses overwrite) and lexicographic order equals
                # chronological order among rotated files.
                target = conv_dir / f"{_safe_conv_name(conversation_id)}-{stamp}-1.jsonl"
                k = 1
                while target.exists():
                    k += 1
                    target = conv_dir / (
                        f"{_safe_conv_name(conversation_id)}-{stamp}-{k}.jsonl"
                    )
                active.rename(target)
            record = {
                "v": 1,
                "call_id": call_id,
                "conversation_id": conversation_id,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "tool": tool,
                "arguments": (
                    arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments, ensure_ascii=False)
                ),
                "ok": bool(result.get("ok", False)),
                "result": result,
            }
            with active.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ── read path ─────────────────────────────────────────────────────────

    def _conv_files(self, conversation_id: str) -> list[Path]:
        """All files for a conversation, oldest first (rotated before active)."""
        prefix = _safe_conv_name(conversation_id)
        files = sorted(self.base_dir.glob(f"{prefix}*.jsonl"))
        # '-' (45) sorts before '.' (46), so rotated files precede the active
        # one alphabetically — this sort IS the chronological order.
        return [p for p in files if p.name.startswith(prefix)]

    def _iter_records(self, conversation_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self._conv_files(conversation_id):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue  # torn tail write — skip
            except OSError:
                continue
        return records

    def get(
        self, conversation_id: str, call_id: str
    ) -> dict[str, Any] | None:
        """Full record for one call id, or None."""
        for rec in self._iter_records(conversation_id):
            if rec.get("call_id") == call_id:
                return rec
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

    # ── retention ─────────────────────────────────────────────────────────

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < _CLEANUP_EVERY_S:
            return
        self._last_cleanup = now
        try:
            cutoff = now - self._retention_days * 86400
            for path in self.base_dir.glob("*.jsonl"):
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
                return ToolResult.failure(
                    f"no archived record for call_id '{call_id}' in this "
                    "session. Use tool_name/keyword search to locate it.",
                    code=404,
                    error_category="not_found",
                )
            return ToolResult.success(self._paginated(rec, offset))

        hits = tool_archive.search(
            conversation_id, tool=tool_name or None, keyword=keyword or None,
            limit=limit,
        )
        if not hits:
            return ToolResult.failure(
                "no matching archived calls in this session "
                "(archive covers this session only).",
                code=404,
                error_category="not_found",
            )
        return ToolResult.success(
            {
                "mode": "search",
                "matches": [
                    {
                        "call_id": h.get("call_id"),
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
