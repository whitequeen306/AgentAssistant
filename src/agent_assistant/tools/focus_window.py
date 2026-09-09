"""focus_window tool — Bring a specified window to the foreground.

Matches by window title pattern. Idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.sanitize import sanitize_error
from agent_assistant.tools.ui_driver import remember_ui_target
from agent_assistant.win_utils import (
    find_windows,
    find_windows_by_exe,
    focus_window_hwnd,
    is_assistant_window,
)

logger = logging.getLogger(__name__)


class FocusWindowTool(Tool):
    @property
    def name(self) -> str:
        return "focus_window"

    @property
    def description(self) -> str:
        return (
            "Bring a specified window to the foreground. "
            "When to call: switch to an already-open app, focus a specific window in a scene mode. "
            "Matches by window title substring (case-insensitive). If the title is a "
            "document/song name rather than the product name, pass the real title from "
            "launch_app.window_title, or the app name (falls back to the registered exe). "
            "When NOT to call: app not running → use launch_app (which already includes focus logic, prefer it); "
            "reading or clicking controls inside the window → use ui_inspect / ui_click / "
            "ui_type / ui_hotkey (focus alone does not interact with the UI)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Window title substring to match (case-insensitive)",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        pattern: str = kwargs.get("title_pattern", "").strip()
        if not pattern:
            return ToolResult.failure("parameter 'title_pattern' is required")

        try:
            windows = [
                w for w in find_windows(pattern) if not is_assistant_window(w)
            ]
            if not windows:
                try:
                    from agent_assistant.app_registry import app_registry

                    app = app_registry.find_app(pattern)
                except Exception:
                    app = None
                if app is not None and app.exe_path:
                    windows = [
                        w
                        for w in find_windows_by_exe(app.exe_path)
                        if not is_assistant_window(w)
                    ]
            if not windows:
                return ToolResult.failure(
                    f"no matching window for pattern '{pattern}'. "
                    "Titles may be the current song/document — try launch_app "
                    "(returns window_title) rather than the product name.",
                    code=404,
                    error_category="not_found",
                )

            target = windows[0]
            focus_window_hwnd(target.hwnd)
            remember_ui_target(target)

            return ToolResult.success(
                data={
                    "focused": target.title,
                    "pid": target.pid,
                    "total_matches": len(windows),
                }
            )
        except Exception as e:
            logger.debug("focus_window failed: %s", e, exc_info=True)
            sanitized = sanitize_error(str(e), context="focus_window", code=500)
            return ToolResult.failure(
                sanitized.safe_message,
                code=500,
                error_category=sanitized.category,
            )
