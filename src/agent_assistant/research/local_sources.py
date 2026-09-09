"""Pending local sources attached to the next deep-research dispatch.

Sources are either knowledge-base notes (under notes_dir) or user-picked
local files. Paths are validated before read (extension + size + resolve).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_assistant.config import settings

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".log", ".rst", ".py", ".markdown"}
MAX_FILE_BYTES = 512 * 1024  # 512 KiB

_lock = threading.Lock()
_pending: list["LocalSource"] = []


@dataclass
class LocalSource:
    kind: Literal["note", "file"]
    path: str  # note filename OR absolute file path
    title: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "title": self.title}


def set_pending_local_sources(sources: list[LocalSource | dict[str, Any]]) -> None:
    parsed: list[LocalSource] = []
    for s in sources:
        if isinstance(s, LocalSource):
            parsed.append(s)
            continue
        if not isinstance(s, dict):
            continue
        kind = s.get("kind")
        path = (s.get("path") or s.get("filename") or "").strip()
        if kind not in ("note", "file") or not path:
            continue
        parsed.append(
            LocalSource(
                kind=kind,  # type: ignore[arg-type]
                path=path,
                title=(s.get("title") or "").strip(),
            )
        )
    with _lock:
        _pending.clear()
        _pending.extend(parsed)


def take_pending_local_sources() -> list[LocalSource]:
    with _lock:
        out = list(_pending)
        _pending.clear()
        return out


def get_pending_local_sources() -> list[LocalSource]:
    """Peek without clearing (main-agent read_attached_source)."""
    with _lock:
        return list(_pending)


def clear_pending_local_sources() -> None:
    with _lock:
        _pending.clear()


def resolve_source_path(source: LocalSource) -> Path | None:
    """Resolve a source to a readable Path, or None if rejected."""
    if source.kind == "note":
        raw = source.path.strip().replace("\\", "/")
        # notes are flat filenames only — reject any directory form
        if not raw or "/" in raw or raw in (".", "..") or Path(raw).name != raw:
            return None
        notes_dir = settings.resolved_notes_dir.resolve()
        path = (notes_dir / raw).resolve()
        if not str(path).startswith(str(notes_dir)) or not path.is_file():
            return None
        return path

    # local file
    try:
        path = Path(source.path).expanduser().resolve()
    except OSError:
        return None
    if not path.is_file():
        return None
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    return path


def load_source_text(source: LocalSource, *, max_chars: int = 12000) -> tuple[bool, str]:
    """Read source text. Returns (ok, text_or_error)."""
    path = resolve_source_path(source)
    if path is None:
        return False, "source rejected (missing, type, size, or path)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, f"read failed: {e}"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…[truncated]"
    return True, text


def format_sources_for_context(sources: list[LocalSource]) -> str:
    """Build a background context block for the research sub-agent."""
    if not sources:
        return ""
    parts = [
        "## Attached local sources",
        "The user enabled local materials for this research. "
        "Call `read_local_source` with the listed path/filename when needed.",
        "",
    ]
    for i, s in enumerate(sources, 1):
        label = s.title or s.path
        parts.append(f"{i}. [{s.kind}] {label}")
        if s.kind == "file":
            parts.append(f"   path: {s.path}")
        else:
            parts.append(f"   filename: {s.path}")
    return "\n".join(parts)
