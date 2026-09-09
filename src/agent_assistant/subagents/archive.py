"""Per-task L0/L1 archives with mandatory recursive secret redaction.

The L0 archive retains full operational detail after sanitization; it is intentionally
not a byte-for-byte raw log and must never be described as containing unredacted input.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent_assistant.tools.sanitize import sanitize_error

from .models import _json_safe

_TASK_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|secret|token|api[-_ ]?key|authorization|cookie",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE),
)
_HEADER_LINE_PATTERN = re.compile(
    r"^(?P<line_prefix>[^\r\n]*?)"
    r"(?P<label>\b(?:Cookie|Authorization)[ \t]*:)[^\r\n]*",
    re.IGNORECASE | re.MULTILINE,
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])(?P<key_quote>[\"']?)"
    r"(?:password|passwd|secret|token|api[-_ ]?key|authorization|cookie)"
    r"(?P=key_quote)\s*(?:=|:)\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s&;,]+)",
    re.IGNORECASE,
)
_WIN_ERROR_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")
_UNIX_ERROR_PATH_PATTERN = re.compile(r"(?:/[\w.\-]+){2,}[^\s\"']*")
_VERSION_PATTERN = re.compile(
    r"\b(?:Python|SQLite)(?:\s+version)?[\s/]+v?\d+(?:\.\d+)+\b",
    re.IGNORECASE,
)
_SQL_PATTERN = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA)\b.*",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_PATTERN = re.compile(r"\btable\s*:?\s*[\w.\-]+", re.IGNORECASE)
_REDACTED = "<redacted>"
_ARCHIVE_LOCKS_GUARD = threading.Lock()
_ARCHIVE_LOCKS: dict[str, threading.RLock] = {}


class TaskArchiveError(RuntimeError):
    """Safe archive error that excludes internal details."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_string(value: str) -> str:
    sanitized = _HEADER_LINE_PATTERN.sub(
        lambda match: (
            f"{match.group('line_prefix')}{match.group('label')} {_REDACTED}"
        ),
        value,
    )
    for pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)

    def replace_assignment(match: re.Match[str]) -> str:
        matched_value = match.group("value")
        if (
            len(matched_value) >= 2
            and matched_value[0] in {"'", '"'}
            and matched_value[-1] == matched_value[0]
        ):
            replacement = f"{matched_value[0]}{_REDACTED}{matched_value[-1]}"
        else:
            replacement = _REDACTED
        return f"{match.group('prefix')}{replacement}"

    return _SENSITIVE_ASSIGNMENT_PATTERN.sub(replace_assignment, sanitized)


def redact_secrets(text: str) -> str:
    """Public string redaction (headers, bearer/sk tokens, inline credentials).

    Shared by the archives and by tools that surface file content snippets.
    """
    return _sanitize_string(text)


def _safe_trace_error(value: Any) -> str:
    try:
        raw = str(value) if isinstance(value, (str, BaseException)) else "unexpected error"
    except Exception:
        raw = "unexpected error"
    prepared = _sanitize_string(raw)
    for pattern in (
        _WIN_ERROR_PATH_PATTERN,
        _UNIX_ERROR_PATH_PATTERN,
        _VERSION_PATTERN,
        _TABLE_PATTERN,
        _SQL_PATTERN,
    ):
        prepared = pattern.sub("<internal detail>", prepared)
    return sanitize_error(prepared, context="subagent_archive").safe_message


def _sanitize(
    value: Any,
    *,
    ui_text: bool = False,
    sanitize_errors: bool = False,
    seen: set[int] | None = None,
) -> Any:
    if isinstance(value, BaseException):
        return "sanitize_error"
    if isinstance(value, str):
        return _sanitize_string(value)

    if isinstance(value, Mapping):
        seen = set() if seen is None else seen
        identity = id(value)
        if identity in seen:
            return "<recursive reference>"
        seen.add(identity)
        try:
            sanitized: dict[Any, Any] = {}
            for key, item in value.items():
                key_text = key.value if hasattr(key, "value") else key
                sensitive = isinstance(key_text, str) and _SENSITIVE_KEY_PATTERN.search(key_text)
                sanitize_ui_text = ui_text and isinstance(key_text, str) and key_text == "text"
                sanitize_trace_error = (
                    sanitize_errors
                    and isinstance(key_text, str)
                    and key_text.casefold() == "error"
                )
                safe_key = _sanitize_string(key) if isinstance(key, str) else key
                sanitized[safe_key] = (
                    _REDACTED
                    if sensitive or sanitize_ui_text
                    else (
                        _safe_trace_error(item)
                        if sanitize_trace_error
                        else _sanitize(
                            item,
                            ui_text=ui_text,
                            sanitize_errors=sanitize_errors,
                            seen=seen,
                        )
                    )
                )
            return sanitized
        finally:
            seen.remove(identity)

    if isinstance(value, (list, tuple, set)):
        seen = set() if seen is None else seen
        identity = id(value)
        if identity in seen:
            return "<recursive reference>"
        seen.add(identity)
        try:
            converted = [
                _sanitize(
                    item,
                    ui_text=ui_text,
                    sanitize_errors=sanitize_errors,
                    seen=seen,
                )
                for item in value
            ]
            return converted if not isinstance(value, tuple) else tuple(converted)
        finally:
            seen.remove(identity)
    return value


class TaskArchive:
    """Manage sanitized L0/L1 files scoped beneath one canonical task directory.

    The four archive artifacts are created lazily. Instances in this process share an
    ``RLock`` per canonical directory and coordinate with other processes through
    ``.archive.lock``. Reparse checks run under those locks immediately before I/O;
    pure ``pathlib`` checks cannot eliminate malicious same-user TOCTOU replacement.
    """

    def __init__(self, root: Path, task_id: str) -> None:
        if (
            not isinstance(task_id, str)
            or _TASK_ID_PATTERN.fullmatch(task_id) is None
            or task_id.casefold() in _WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("task_id must be a canonical safe identifier")
        self.root = Path(root)
        self.task_id = task_id
        self.directory = self.root / task_id
        lock_key = os.path.normcase(os.path.abspath(self.directory))
        with _ARCHIVE_LOCKS_GUARD:
            self._lock = _ARCHIVE_LOCKS.setdefault(lock_key, threading.RLock())
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                self._reject_reparse(self.root)
                self.directory.mkdir(exist_ok=True)
                self._validate_paths()
        except OSError:
            raise TaskArchiveError("Unable to initialize task archive") from None
        self.raw_path = self.directory / "raw.jsonl"
        self.trace_path = self.directory / "trace.jsonl"
        self.checkpoint_path = self.directory / "checkpoint.json"
        self.output_path = self.directory / "output.md"
        self._lock_path = self.directory / ".archive.lock"

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return path.is_symlink() or bool(attributes & reparse_flag)

    @classmethod
    def _reject_reparse(cls, path: Path) -> None:
        try:
            if cls._is_reparse(path):
                raise TaskArchiveError("Archive path is unsafe")
        except OSError:
            raise TaskArchiveError("Archive path is unsafe") from None

    def _validate_paths(self, *leaves: Path) -> None:
        try:
            self._reject_reparse(self.root)
            self._reject_reparse(self.directory)
            root_resolved = self.root.resolve(strict=True)
            directory_resolved = self.directory.resolve(strict=True)
            if not directory_resolved.is_relative_to(root_resolved):
                raise TaskArchiveError("Archive path is unsafe")
            for leaf in leaves:
                self._reject_reparse(leaf)
                if not leaf.resolve(strict=False).is_relative_to(root_resolved):
                    raise TaskArchiveError("Archive path is unsafe")
        except (OSError, RuntimeError):
            raise TaskArchiveError("Archive path is unsafe") from None

    @staticmethod
    def _lock_file(handle: Any) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock_file(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _archive_guard(self, *targets: Path) -> Iterator[None]:
        with self._lock:
            self._validate_paths(self._lock_path, *targets)
            try:
                handle = self._lock_path.open("a+b")
            except OSError:
                raise TaskArchiveError("Unable to lock task archive") from None
            locked = False
            try:
                self._lock_file(handle)
                locked = True
                self._validate_paths(self._lock_path, *targets)
            except OSError:
                if locked:
                    try:
                        self._unlock_file(handle)
                    except OSError:
                        pass
                handle.close()
                raise TaskArchiveError("Unable to access task archive") from None
            except BaseException:
                if locked:
                    try:
                        self._unlock_file(handle)
                    except OSError:
                        pass
                handle.close()
                raise
            try:
                yield
            finally:
                if locked:
                    try:
                        self._unlock_file(handle)
                    except OSError:
                        pass
                handle.close()

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        line = (
            json.dumps(
                _json_safe(record),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        try:
            with self._archive_guard(path):
                self._validate_paths(path)
                with path.open("ab") as handle:
                    if handle.write(line) != len(line):
                        raise OSError("short archive write")
                    handle.flush()
        except OSError:
            raise TaskArchiveError("Unable to write task archive") from None

    def log_raw(self, tool: str, arguments: Any, result: Any) -> None:
        """Append detailed L0 data after mandatory sanitization."""

        record = {
            "timestamp": _utc_now(),
            "tool": _sanitize_string(str(tool)),
            "arguments": _sanitize(arguments, ui_text=tool == "ui_type"),
            "result": _sanitize(result),
        }
        self._append_jsonl(self.raw_path, record)

    def log_trace(self, event: str, data: Mapping[str, Any] | None = None) -> None:
        """Append a concise L1 event without stack traces or exception details."""

        record = {
            "timestamp": _utc_now(),
            "event": _sanitize_string(str(event)),
            "data": _sanitize({} if data is None else data, sanitize_errors=True),
        }
        self._append_jsonl(self.trace_path, record)

    def write_checkpoint(self, checkpoint: Any) -> None:
        """Atomically replace the sanitized checkpoint."""

        content = json.dumps(
            _json_safe(_sanitize(checkpoint)),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        self._atomic_write(self.checkpoint_path, content, "Unable to write task checkpoint")

    def read_checkpoint(self) -> Any:
        """Read the latest checkpoint, returning ``None`` when it does not exist."""

        try:
            with self._archive_guard(self.checkpoint_path):
                if not self.checkpoint_path.exists():
                    return None
                self._validate_paths(self.checkpoint_path)
                return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise TaskArchiveError("Stored task checkpoint is invalid") from None

    def _atomic_write(self, target: Path, content: str, error_message: str) -> None:
        temporary: Path | None = None
        try:
            with self._archive_guard(target):
                self._validate_paths(target)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.directory,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._validate_paths(target, temporary)
                os.replace(temporary, target)
                temporary = None
        except OSError:
            raise TaskArchiveError(error_message) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def write_output(self, output: str) -> None:
        """Atomically replace sanitized human-readable output."""

        if not isinstance(output, str):
            raise TypeError("output must be a string")
        self._atomic_write(
            self.output_path,
            _sanitize_string(output),
            "Unable to write task output",
        )

