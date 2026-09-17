"""Session trace — per-session event stream + full tool details.

Two files, one directory per session::

    <data_dir>/sessions/<session_id>/
    ├── events.jsonl         # 事件流：发生了什么（11 类事件，一句话摘要）
    └── tool_details.jsonl   # 完整工具参数 + 完整结果（不截断，带 T-xxxx ID）

They answer two different questions and are therefore split apart (same
reasoning as L0/L1 in the research module):

* ``events.jsonl``      — "这一轮发生了什么？"  Compact timeline: turn
  boundaries, user/llm/tool events, **compaction and pruning** (so an
  auditor can explain *why* a piece of context disappeared), recall usage,
  token snapshots, model info.  Every line carries ``ok`` so failures are
  machine-readable instead of buried in prose.
* ``tool_details.jsonl`` — "当时到底看到了什么？"  The full arguments and
  the full result of every tool call.  ``recall_tool_result`` reads this
  file, so the model can pull back exactly what compaction shrank.

Both writers are **fire-and-forget**: a trace failure must never break tool
execution or a turn (same contract as ``tools/tool_archive.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_assistant.config import settings

logger = logging.getLogger(__name__)

# Active file rotates aside past this size so reads stay bounded.
_MAX_FILE_BYTES = 5 * 1024 * 1024
# Results bigger than this go to their own file; the record keeps a pointer.
_MAX_INLINE_RESULT_BYTES = 1024 * 1024
# Event-specific payloads are capped so a runaway value can't bloat the stream.
_EXTRA_MAX_CHARS = 2000
# Summary fields in the event stream stay one line.
_SUMMARY_MAX_CHARS = 400

# Event types (see docs/context-management-refactor.md §5).
EVENT_SESSION_START = "session_start"
EVENT_SESSION_END = "session_end"
EVENT_TURN_START = "turn_start"
EVENT_TURN_END = "turn_end"
EVENT_USER = "user"
EVENT_LLM = "llm"
EVENT_TOOL = "tool"
EVENT_LLM_ERROR = "llm_error"
EVENT_COMPACTION = "compaction"
EVENT_PRUNING = "pruning"
EVENT_SUMMARY_ARCHIVED = "summary_archive"
EVENT_RECALL = "recall"


def safe_session_name(session_id: str) -> str:
    """Filesystem-safe session directory name (collision-safe)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id or "").strip("_")
    safe = safe[:80] or "session"
    if safe != session_id:
        digest = hashlib.md5((session_id or "").encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{digest}"
    return safe


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SessionTrace:
    """Append-only per-session event stream + tool detail store."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        max_file_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self._base_dir = base_dir
        self._max_file_bytes = max_file_bytes
        self._lock = threading.Lock()
        # Per-session tool-detail counter (id sequence). Seeded by scanning the
        # file once, then incremented in memory.
        self._seq: dict[str, int] = {}

    @property
    def base_dir(self) -> Path:
        return self._base_dir or (settings.data_dir / "sessions")

    def session_dir(self, session_id: str) -> Path:
        return self.base_dir / safe_session_name(session_id)

    # ── write path ────────────────────────────────────────────────────────

    def event(
        self,
        session_id: str,
        event: str,
        *,
        turn: int | None = None,
        ok: bool = True,
        error_category: str | None = None,
        summary: str = "",
        detail_ref: str | None = None,
        tokens: dict[str, Any] | None = None,
        model: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """Append one event line. Never raises (logs on failure)."""
        try:
            record: dict[str, Any] = {
                "v": 1,
                "ts": _now(),
                "session_id": session_id,
                "turn": turn,
                "event": event,
                "ok": bool(ok),
                "error_category": error_category,
                "summary": _clip(summary, _SUMMARY_MAX_CHARS) if summary else "",
            }
            if detail_ref:
                record["detail_ref"] = detail_ref
            if tokens:
                record["tokens"] = tokens
            if model:
                record["model"] = model
            if extra:
                clean = {k: v for k, v in extra.items() if v is not None}
                if clean:
                    record["extra"] = self._bounded_extra(clean)
            self._append(session_id, "events.jsonl", record)
        except Exception:
            logger.warning(
                "session trace event write failed (event=%s session=%s)",
                event,
                session_id,
                exc_info=True,
            )

    @staticmethod
    def _bounded_extra(extra: dict[str, Any]) -> dict[str, Any]:
        """Keep one-record payloads small without dropping the structure."""
        encoded = json.dumps(extra, ensure_ascii=False, default=str)
        if len(encoded) <= _EXTRA_MAX_CHARS:
            return extra
        return {"_truncated": True, "_preview": encoded[: _EXTRA_MAX_CHARS - 40] + "…"}

    def tool_detail(
        self,
        session_id: str,
        *,
        call_id: str,
        tool: str,
        arguments: str | dict[str, Any],
        ok: bool,
        result: dict[str, Any],
        turn: int | None = None,
    ) -> str | None:
        """Append the FULL call + result; returns its ``T-xxxx`` id.

        Never raises — returns None if the write failed.
        """
        try:
            with self._lock:
                detail_id = self._next_id(session_id)
                stored: dict[str, Any] = result
                encoded = json.dumps(result, ensure_ascii=False, default=str)
                if len(encoded.encode("utf-8")) > _MAX_INLINE_RESULT_BYTES:
                    stored = self._spill_to_file(session_id, detail_id, result, encoded)
                record = {
                    "v": 1,
                    "id": detail_id,
                    "session_id": session_id,
                    "call_id": call_id,
                    "turn": turn,
                    "ts": _now(),
                    "tool": tool,
                    "arguments": (
                        arguments
                        if isinstance(arguments, str)
                        else json.dumps(arguments, ensure_ascii=False, default=str)
                    ),
                    "ok": bool(ok),
                    "result": stored,
                }
                self._append_locked(session_id, "tool_details.jsonl", record)
                return detail_id
        except Exception:
            logger.warning(
                "session trace tool-detail write failed (tool=%s session=%s)",
                tool,
                session_id,
                exc_info=True,
            )
            return None

    def _next_id(self, session_id: str) -> str:
        """``T-0001``-style id, unique per session."""
        seq = self._seq.get(session_id)
        if seq is None:
            seq = self._count_existing_details(session_id)
        seq += 1
        self._seq[session_id] = seq
        return f"T-{seq:04d}"

    def _count_existing_details(self, session_id: str) -> int:
        total = 0
        for path in self._detail_files(session_id):
            try:
                with path.open("r", encoding="utf-8") as f:
                    total += sum(1 for line in f if line.strip())
            except OSError:
                continue
        return total

    def _spill_to_file(
        self, session_id: str, detail_id: str, result: dict[str, Any], encoded: str
    ) -> dict[str, Any]:
        """Oversized result → own file; the record keeps a readable pointer."""
        details_dir = self.session_dir(session_id) / "details"
        details_dir.mkdir(parents=True, exist_ok=True)
        target = details_dir / f"{detail_id}.json"
        target.write_text(encoded, encoding="utf-8")
        return {
            "_ref": f"details/{detail_id}.json",
            "_bytes": len(encoded.encode("utf-8")),
            "_preview": encoded[:400],
        }

    def _append(self, session_id: str, filename: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._append_locked(session_id, filename, record)

    def _append_locked(
        self, session_id: str, filename: str, record: dict[str, Any]
    ) -> None:
        session_dir = self.session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        active = session_dir / filename
        if active.exists() and active.stat().st_size >= self._max_file_bytes:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            stem = filename[: -len(".jsonl")]
            target = session_dir / f"{stem}-{stamp}-1.jsonl"
            k = 1
            # Always suffix -N: unique within the same second (Windows rename
            # refuses overwrite) and lexicographic order == chronological.
            while target.exists():
                k += 1
                target = session_dir / f"{stem}-{stamp}-{k}.jsonl"
            active.rename(target)
        with active.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # ── read path ─────────────────────────────────────────────────────────

    def _files_for(self, session_id: str, filename: str) -> list[Path]:
        """Rotated files first, then the active one (== chronological order).

        '-' (45) sorts before '.' (46), so ``stem-<stamp>-N.jsonl`` precedes
        ``stem.jsonl`` alphabetically. A plain name sort is the time order.
        """
        session_dir = self.session_dir(session_id)
        if not session_dir.is_dir():
            return []
        stem = filename[: -len(".jsonl")]
        files = sorted(session_dir.glob(f"{stem}*.jsonl"))
        return [p for p in files if p.is_file()]

    def _detail_files(self, session_id: str) -> list[Path]:
        return self._files_for(session_id, "tool_details.jsonl")

    def iter_events(self, session_id: str) -> list[dict[str, Any]]:
        return self._read(self._files_for(session_id, "events.jsonl"))

    def iter_tool_details(self, session_id: str) -> list[dict[str, Any]]:
        return self._read(self._detail_files(session_id))

    @staticmethod
    def _read(paths: list[Path]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in paths:
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

    def read_detail_file(self, session_id: str, ref: str) -> str | None:
        """Contents of a spilled detail file, or None (path-guarded)."""
        try:
            target = (self.session_dir(session_id) / ref).resolve()
            if not str(target).startswith(str(self.session_dir(session_id).resolve())):
                return None
            return target.read_text(encoding="utf-8")
        except OSError:
            return None


# Global singleton — the loop writes through this; tests construct their own
# or monkeypatch ``agent_assistant.agent.loop.session_trace``.
session_trace = SessionTrace()


def trace_recall(
    mode: str,
    ok: bool,
    detail: str,
    *,
    tool: str = "recall_tool_result",
) -> None:
    """Audit trail: which previously-archived content the model pulled back.

    Answers "the model quoted a number from 40 turns ago — where did that come
    from?" after the fact. Best-effort: never raises.
    """
    try:
        from agent_assistant.subagents.context import current_execution_context

        context = current_execution_context()
        session_id = context.conversation_id if context is not None else "local"
        session_trace.event(
            session_id,
            EVENT_RECALL,
            ok=ok,
            summary=f"{tool}[{mode}] {detail}",
            tool=tool,
            mode=mode,
        )
    except Exception:  # pragma: no cover — tracing must never break a tool
        logger.debug("recall trace failed (tool=%s)", tool, exc_info=True)
