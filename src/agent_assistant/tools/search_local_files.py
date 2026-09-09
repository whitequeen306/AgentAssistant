"""Bounded, jail-aware local file search (Explorer's primary tool).

Hard bounds by design: jail-rooted, no symlink/reparse traversal, capped
visited entries, a wall-clock deadline, size-capped UTF-8-only content
scanning, and secret-redacted snippets.
"""

from __future__ import annotations

import fnmatch
import os
import stat as stat_module
import time
from pathlib import Path
from typing import Any

from agent_assistant.subagents.archive import redact_secrets
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.permission import path_in_allowed_roots

_MAX_RESULTS_CAP = 200
_MAX_VISITED_ENTRIES = 20000
_DEADLINE_SECONDS = 10.0
_MAX_CONTENT_BYTES = 1024 * 1024  # 1 MiB
_SNIPPET_CHARS = 240

# Only these extensions are ever content-scanned (plain-text formats).
CONTENT_SCAN_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".jsonl",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".html",
        ".htm", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".bat", ".ps1",
        ".sh", ".sql", ".rst", ".tex",
    }
)


def _is_reparse_or_symlink(entry_path: str, entry: os.DirEntry | None = None) -> bool:
    """True for symlinks and Windows reparse points (junctions, OneDrive stubs)."""
    try:
        if entry is not None and entry.is_symlink():
            return True
        metadata = os.lstat(entry_path)
    except OSError:
        return True  # unreadable — treat as unsafe, skip
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or stat_module.S_ISLNK(metadata.st_mode)


def _content_snippet(path: Path, query: str) -> str | None:
    """First redacted snippet around a case-insensitive content match."""
    try:
        if path.stat().st_size > _MAX_CONTENT_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None  # not UTF-8-compatible → never scanned
    lowered = text.casefold()
    index = lowered.find(query.casefold())
    if index < 0:
        return None
    start = max(0, index - 60)
    snippet = text[start : start + _SNIPPET_CHARS].replace("\r", " ").replace("\n", " ")
    return redact_secrets(snippet.strip())


class SearchLocalFilesTool(Tool):
    @property
    def name(self) -> str:
        return "search_local_files"

    @property
    def description(self) -> str:
        return (
            "Search local files under one directory by name (always) and "
            "optionally by text content. When to call: find documents by "
            "keyword (e.g. 考研资料), locate files whose name or content "
            "mentions a topic. When NOT to call: listing one known directory "
            "(use list_files); reading a known file (use read_file). Bounded: "
            "stays inside the user's allowed directories, skips shortcuts/"
            "symlinks and binary files, and stops at a time/entry budget. "
            "Content matching only scans UTF-8 text files under 1 MiB."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="root", type="string", description="Directory to search under"),
            ToolParameter(
                name="query",
                type="string",
                description="Keyword matched against file names (and content)",
            ),
            ToolParameter(
                name="pattern",
                type="string",
                description="Glob filter e.g. '*.pdf' (default '*')",
                required=False,
                default="*",
            ),
            ToolParameter(
                name="search_content",
                type="boolean",
                description="Also scan text-file content (default false)",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="max_results",
                type="integer",
                description=f"Result cap, 1-{_MAX_RESULTS_CAP} (default 50)",
                required=False,
                default=50,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        root_str = str(kwargs.get("root") or "").strip()
        query = str(kwargs.get("query") or "").strip()
        pattern = str(kwargs.get("pattern") or "*").strip() or "*"
        search_content = bool(kwargs.get("search_content", False))
        max_results_raw = kwargs.get("max_results", 50)

        if not root_str:
            return ToolResult.failure("parameter 'root' is required", code=400)
        if not query:
            return ToolResult.failure("parameter 'query' is required", code=400)
        if not isinstance(max_results_raw, int) or isinstance(max_results_raw, bool):
            return ToolResult.failure("max_results must be an integer", code=400)
        if max_results_raw < 1 or max_results_raw > _MAX_RESULTS_CAP:
            return ToolResult.failure(
                f"max_results must be between 1 and {_MAX_RESULTS_CAP}", code=400
            )
        max_results = max_results_raw

        try:
            root = Path(root_str).expanduser().resolve(strict=True)
        except OSError:
            return ToolResult.failure("root directory not found", code=404)
        if not root.is_dir():
            return ToolResult.failure("root is not a directory", code=400)
        if not path_in_allowed_roots(str(root)):
            return ToolResult.failure(
                "root is outside the allowed directories (file jail)",
                code=403,
                error_category="permission_denied",
            )
        if _is_reparse_or_symlink(str(root)):
            return ToolResult.failure("root is a symlink/reparse point", code=400)

        results: list[dict[str, Any]] = []
        visited = 0
        truncated = False
        deadline = time.monotonic() + _DEADLINE_SECONDS
        query_folded = query.casefold()
        stack: list[str] = [str(root)]

        while stack:
            if time.monotonic() > deadline or visited >= _MAX_VISITED_ENTRIES:
                truncated = True
                break
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    visited += 1
                    if visited >= _MAX_VISITED_ENTRIES or time.monotonic() > deadline:
                        truncated = True
                        break
                    if _is_reparse_or_symlink(entry.path, entry):
                        continue  # never traverse or report link targets
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    name = entry.name
                    if not fnmatch.fnmatch(name, pattern):
                        continue

                    match_kind: str | None = None
                    snippet: str | None = None
                    if query_folded in name.casefold():
                        match_kind = "name"
                    elif search_content:
                        suffix = os.path.splitext(name)[1].casefold()
                        if suffix in CONTENT_SCAN_EXTENSIONS:
                            snippet = _content_snippet(Path(entry.path), query)
                            if snippet is not None:
                                match_kind = "content"
                    if match_kind is None:
                        continue

                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    results.append(
                        {
                            "path": str(Path(entry.path)),
                            "name": name,
                            "size": info.st_size,
                            "modified": int(info.st_mtime),
                            "match": match_kind,
                            **({"snippet": snippet} if snippet else {}),
                        }
                    )
                    if len(results) >= max_results:
                        truncated = True
                        stack.clear()
                        break

        return ToolResult.success(
            data={
                "root": str(root),
                "query": query,
                "results": results,
                "count": len(results),
                "visited_entries": visited,
                "truncated": truncated,
            },
            warning=(
                "search stopped early (time/entry/result budget); narrow the "
                "root or pattern for full coverage"
                if truncated
                else None
            ),
        )
