"""save_note tool — Save content as a local Markdown note.

Unified wrap-up action: translate/summarize/ask/deep-research/morning-briefing
results can all call this to persist output.

Storage: ~/AgentAssistant/notes/ with frontmatter (title/tag/timestamp).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_assistant.config import settings
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult


def _sanitize_filename(title: str) -> str:
    """Remove illegal path characters from title."""
    # Replace illegal chars with underscore
    sanitized = re.sub(r'[<>:"/\\|?*]', "_", title)
    # Trim whitespace and dots
    sanitized = sanitized.strip(". ")
    # Limit length
    if len(sanitized) > 100:
        sanitized = sanitized[:100]
    return sanitized or "untitled"


class SaveNoteTool(Tool):
    @property
    def name(self) -> str:
        return "save_note"

    @property
    def requires_confirm(self) -> bool:
        # Framework gate — UI must approve; model cannot bypass.
        return True

    @property
    def description(self) -> str:
        return (
            "Save content as a local Markdown note with title/tag/timestamp. "
            "CRITICAL: Never call this silently. First ask the user in chat "
            "whether they want the content saved as a note; only call AFTER "
            "they explicitly agree (e.g. 好/可以/保存吧). The UI will also "
            "show a confirmation dialog before writing. "
            "When to call: user agreed to persist translate/summarize/ask/"
            "deep-research/briefing output. "
            "When NOT to call: user did not ask to save; modifying an existing "
            "note in 资料库 (use the Library edit UI); speculative archival. "
            "Available to: main agent + sub-agent."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="content",
                type="string",
                description="The note content (Markdown supported)",
            ),
            ToolParameter(
                name="title",
                type="string",
                description="Note title (used as filename)",
            ),
            ToolParameter(
                name="tag",
                type="string",
                description="Optional tag/category for the note",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        content: str = kwargs.get("content", "").strip()
        title: str = kwargs.get("title", "").strip()
        tag: str = kwargs.get("tag", "").strip()

        if not content:
            return ToolResult.failure("parameter 'content' is required and cannot be empty")
        if not title:
            return ToolResult.failure("parameter 'title' is required and cannot be empty")

        # Ensure notes directory exists
        notes_dir = settings.resolved_notes_dir
        notes_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp to avoid collisions
        timestamp = datetime.now()
        safe_title = _sanitize_filename(title)
        filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{safe_title}.md"
        filepath = notes_dir / filename

        # Build Markdown with frontmatter
        frontmatter_lines = [
            "---",
            f"title: \"{title}\"",
            f"date: {timestamp.isoformat()}",
        ]
        if tag:
            frontmatter_lines.append(f"tag: \"{tag}\"")
        frontmatter_lines.append("---")
        frontmatter_lines.append("")

        full_content = "\n".join(frontmatter_lines) + content + "\n"

        try:
            filepath.write_text(full_content, encoding="utf-8")
        except OSError as e:
            return ToolResult.failure(f"failed to write note: {e}", code=500)

        return ToolResult.success(
            data={
                "path": str(filepath),
                "filename": filename,
                "title": title,
                "tag": tag or None,
            }
        )


class ReadNoteTool(Tool):
    """Read a saved note from the notes library (agent-facing, J23-adjacent).

    Lets the agent revisit what it (or the user) previously saved — the
    quiz/practice flow depends on it. Path-safe: bare filenames only.
    """

    @property
    def name(self) -> str:
        return "read_note"

    @property
    def description(self) -> str:
        return (
            "Read one saved note from the user's notes library (Markdown). "
            "When to call: the user asks about a note they saved, quiz/practice "
            "on saved material, or reviewing earlier findings. "
            "When NOT to call: general local files → read_file; "
            "uploaded knowledge-base docs → search_knowledge. "
            "Titles may map to timestamped filenames — ask or list when unsure."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="filename",
                type="string",
                description="Note filename (.md) as saved, e.g. 20260909_120000_笔记.md",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        filename = (kwargs.get("filename") or "").strip().replace("\\", "/").split("/")[-1]
        if not filename:
            return ToolResult.failure("parameter 'filename' is required")
        if not filename.endswith(".md"):
            filename += ".md"

        notes_dir = settings.resolved_notes_dir.resolve()
        path = (notes_dir / filename).resolve()
        if not str(path).startswith(str(notes_dir)) or not path.is_file():
            return ToolResult.failure(
                f"note '{filename}' not found in the library", code=404
            )
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult.failure(f"failed to read note: {e}", code=500)
        return ToolResult.success(data={
            "filename": filename,
            "content": content[:60_000],
            "truncated": len(content) > 60_000,
        })
