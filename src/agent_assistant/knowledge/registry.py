"""JSON registry of ingested knowledge-base files (file-level metadata)."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()


@dataclass
class KnowledgeFile:
    file_id: str
    title: str
    filename: str
    stored_path: str
    size: int
    chunk_count: int
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnowledgeFile:
        return cls(
            file_id=str(data["file_id"]),
            title=str(data.get("title") or data.get("filename") or ""),
            filename=str(data.get("filename") or ""),
            stored_path=str(data.get("stored_path") or ""),
            size=int(data.get("size") or 0),
            chunk_count=int(data.get("chunk_count") or 0),
            created_at=float(data.get("created_at") or time.time()),
        )


class KnowledgeRegistry:
    """Persist file metadata beside the vector store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._files: dict[str, KnowledgeFile] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            self._files = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            items = raw.get("files", raw if isinstance(raw, list) else [])
            self._files = {
                f.file_id: f
                for item in items
                if isinstance(item, dict)
                for f in [KnowledgeFile.from_dict(item)]
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning("Knowledge registry load failed: %s", e)
            self._files = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "files": [f.to_dict() for f in sorted(
                self._files.values(), key=lambda x: x.created_at, reverse=True
            )]
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def list_files(self) -> list[KnowledgeFile]:
        with _lock:
            return sorted(self._files.values(), key=lambda x: x.created_at, reverse=True)

    def get(self, file_id: str) -> KnowledgeFile | None:
        with _lock:
            return self._files.get(file_id)

    def add(self, file: KnowledgeFile) -> None:
        with _lock:
            self._files[file.file_id] = file
            self._save()

    def remove(self, file_id: str) -> KnowledgeFile | None:
        with _lock:
            removed = self._files.pop(file_id, None)
            if removed:
                self._save()
            return removed

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]
