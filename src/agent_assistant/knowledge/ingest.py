"""Text cleaning + helpers for knowledge-base ingest."""

from __future__ import annotations

import re
from pathlib import Path

ALLOWED_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".log",
    ".rst",
    ".py",
    ".markdown",
}
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB


def clean_text(text: str) -> str:
    """Normalize raw file text before chunking."""
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_filename(name: str, *, max_len: int = 80) -> str:
    """Sanitize a basename for storage (no path separators)."""
    base = Path(name).name.strip() or "document"
    base = re.sub(r"[^\w.\-一-龥]+", "_", base, flags=re.UNICODE)
    base = base.strip("._") or "document"
    return base[:max_len]


def validate_source_path(path: Path) -> tuple[bool, str]:
    """Check path is a readable allowed file under size limit."""
    try:
        resolved = path.expanduser().resolve()
    except OSError as e:
        return False, f"invalid path: {e}"
    if not resolved.is_file():
        return False, "not a file"
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        return False, f"unsupported type: {resolved.suffix or '(none)'}"
    try:
        size = resolved.stat().st_size
    except OSError as e:
        return False, f"stat failed: {e}"
    if size > MAX_FILE_BYTES:
        return False, f"file too large (max {MAX_FILE_BYTES // (1024 * 1024)} MiB)"
    if size == 0:
        return False, "empty file"
    return True, ""
