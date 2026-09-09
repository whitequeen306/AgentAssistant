"""Per-turn context attachment: library notes XOR knowledge RAG + optional local files."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from agent_assistant.research.local_sources import LocalSource, set_pending_local_sources

PrimarySource = Literal["none", "notes", "knowledge"]

_lock = threading.Lock()
_use_knowledge = False


@dataclass
class ContextAttachment:
    """What the user enabled for the current chat turn."""

    primary: PrimarySource = "none"
    notes: list[LocalSource] = field(default_factory=list)
    files: list[LocalSource] = field(default_factory=list)

    @property
    def use_knowledge(self) -> bool:
        return self.primary == "knowledge"

    @property
    def attached_sources(self) -> list[LocalSource]:
        """Notes + local files (readable via read_attached_source / research)."""
        return [*self.notes, *self.files]


def parse_context_attachment(raw: Any) -> ContextAttachment:
    """Parse bridge/JS payload into ContextAttachment."""
    if not isinstance(raw, dict):
        return ContextAttachment()

    primary = raw.get("primary") or "none"
    if primary not in ("none", "notes", "knowledge"):
        primary = "none"

    notes: list[LocalSource] = []
    files: list[LocalSource] = []

    for item in raw.get("notes") or []:
        if not isinstance(item, dict):
            continue
        path = (item.get("path") or item.get("filename") or "").strip()
        if not path:
            continue
        notes.append(
            LocalSource(kind="note", path=path, title=(item.get("title") or "").strip())
        )

    for item in raw.get("files") or []:
        if not isinstance(item, dict):
            continue
        path = (item.get("path") or "").strip()
        if not path:
            continue
        files.append(
            LocalSource(kind="file", path=path, title=(item.get("title") or "").strip())
        )

    # Legacy: flat research_sources list
    for item in raw.get("sources") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        path = (item.get("path") or item.get("filename") or "").strip()
        if kind == "note" and path:
            notes.append(
                LocalSource(kind="note", path=path, title=(item.get("title") or "").strip())
            )
        elif kind == "file" and path:
            files.append(
                LocalSource(kind="file", path=path, title=(item.get("title") or "").strip())
            )

    if primary == "notes" and not notes:
        primary = "none"
    if primary == "knowledge":
        notes = []  # exclusive with notes

    return ContextAttachment(primary=primary, notes=notes, files=files)  # type: ignore[arg-type]


def apply_context_attachment(att: ContextAttachment) -> None:
    """Park sources + knowledge flag for tools / research sub-agent."""
    set_pending_local_sources(att.attached_sources)
    with _lock:
        global _use_knowledge
        _use_knowledge = att.use_knowledge


def take_use_knowledge() -> bool:
    """Read-and-clear knowledge flag (for dispatch_research)."""
    with _lock:
        global _use_knowledge
        flag = _use_knowledge
        _use_knowledge = False
        return flag


def peek_use_knowledge() -> bool:
    with _lock:
        return _use_knowledge


def clear_context_attachment() -> None:
    from agent_assistant.research.local_sources import clear_pending_local_sources

    clear_pending_local_sources()
    with _lock:
        global _use_knowledge
        _use_knowledge = False


def format_attachment_hint(att: ContextAttachment) -> str:
    """System hint appended to the user message for the agent."""
    parts: list[str] = []
    if att.use_knowledge:
        parts.append(
            "[System] User enabled the knowledge base for this turn. "
            "Call search_knowledge to retrieve relevant uploaded documents "
            "(hybrid retrieval + rerank)."
        )
    if att.notes:
        names = ", ".join((n.title or n.path) for n in att.notes[:12])
        parts.append(
            f"[System] User attached library notes ({len(att.notes)}): {names}. "
            "Call read_attached_source with the note filename when needed."
        )
    if att.files:
        names = ", ".join((f.title or f.path) for f in att.files[:12])
        parts.append(
            f"[System] User attached local files ({len(att.files)}): {names}. "
            "Call read_attached_source with the path when needed. "
            "If you dispatch_research, these are also queued for the sub-agent."
        )
    return "\n\n".join(parts)
