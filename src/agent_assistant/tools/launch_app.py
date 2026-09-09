"""launch_app tool — Launch an app or focus an already-running instance.

Idempotent: if the window already exists, focuses instead of relaunching.
"""

from __future__ import annotations

from typing import Any

from agent_assistant.app_registry import app_registry
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.ui_driver import remember_ui_target
from agent_assistant.win_utils import (
    find_windows,
    find_windows_by_exe,
    focus_window_hwnd,
    is_assistant_window,
    launch_process,
    launch_protocol_uri,
    wait_for_process_window,
)


class LaunchAppTool(Tool):
    @property
    def name(self) -> str:
        return "launch_app"

    @property
    def description(self) -> str:
        return (
            "Launch an app or focus an already-running instance. "
            "When to call: user says 'open X' / 'start Y'. "
            "Looks up the local registry first; if missing, discovers the app on this PC "
            "(Start Menu shortcuts, PATH, common install folders) and auto-registers it. "
            "If the window already exists, it focuses instead of relaunching (idempotent). "
            "Returns the real window_title (players often show the current song, not the "
            "product name) — pass that to ui_inspect/ui_click/ui_type title_pattern. "
            "When NOT to call: switching to an already-open window → use focus_window (lighter); "
            "running a CLI program → use run_command; "
            "interacting inside an already-open app (click/type/hotkey) → use ui_inspect / "
            "ui_click / ui_type / ui_hotkey (do not fake UI clicks via run_command)."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="name",
                type="string",
                description="App name or alias (e.g. '逆战' / 'Cursor' / 'Chrome')",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        name: str = kwargs.get("name", "").strip()
        if not name:
            return ToolResult.failure("parameter 'name' is required")

        # Registry first; on miss, scan this PC and auto-register.
        app = app_registry.discover_app(name)
        if app is None:
            return ToolResult.failure(
                f"在本机未找到应用「{name}」。"
                "可换常用名再试，或提供 exe 完整路径 / 协议 URI（如 qoder://）后重试。",
                code=404,
            )
        if not app.exe_path and not app.protocol_uri:
            return ToolResult.failure(
                f"找到了「{app.name}」但没有可启动路径。"
                "请提供 exe 完整路径或协议 URI。",
                code=404,
            )

        # Check if already running (title pattern often fails for players whose
        # HWND title is the current track — fall back to the exe's windows).
        windows = []
        if app.window_title_pattern:
            windows = [
                w
                for w in find_windows(app.window_title_pattern)
                if not is_assistant_window(w)
            ]
        if not windows and app.exe_path:
            windows = [
                w for w in find_windows_by_exe(app.exe_path) if not is_assistant_window(w)
            ]
        if windows:
            target = windows[0]
            focus_window_hwnd(target.hwnd)
            remember_ui_target(target)
            return ToolResult.success(
                data={
                    "action": "focused",
                    "app": app.name,
                    "window_title": target.title,
                    "pid": target.pid,
                    "hint": (
                        "Use this window_title as ui_inspect/ui_click "
                        "title_pattern — it may be a document/song name, not the app name."
                    ),
                }
            )

        # Not running → launch
        if app.protocol_uri:
            try:
                launch_protocol_uri(app.protocol_uri)
                found = None
                if app.exe_path:
                    found = wait_for_process_window(app.exe_path, timeout_s=8.0)
                if found is not None:
                    focus_window_hwnd(found.hwnd)
                    remember_ui_target(found)
                    return ToolResult.success(
                        data={
                            "action": "launched",
                            "app": app.name,
                            "method": "protocol_uri",
                            "window_title": found.title,
                            "pid": found.pid,
                        }
                    )
                return ToolResult.success(
                    data={
                        "action": "launched",
                        "app": app.name,
                        "method": "protocol_uri",
                    }
                )
            except Exception as e:
                return ToolResult.failure(f"failed to launch via protocol URI: {e}")

        if app.exe_path:
            try:
                pid = launch_process(app.exe_path, workdir=app.workdir)
                found = wait_for_process_window(app.exe_path, timeout_s=8.0)
                if found is not None:
                    focus_window_hwnd(found.hwnd)
                    remember_ui_target(found)
                    return ToolResult.success(
                        data={
                            "action": "launched",
                            "app": app.name,
                            "pid": found.pid,
                            "window_title": found.title,
                            "method": "exe",
                            "hint": (
                                "Use window_title as ui_* title_pattern "
                                "(may be a song/document name, not the product name)."
                            ),
                        }
                    )
                return ToolResult.success(
                    data={
                        "action": "launched",
                        "app": app.name,
                        "pid": pid,
                        "method": "exe",
                        "window_title": None,
                        "hint": (
                            "Window not visible yet. Retry launch_app to focus, "
                            "or ui_inspect with the real window title (not the app name)."
                        ),
                    }
                )
            except Exception as e:
                return ToolResult.failure(f"failed to launch '{app.exe_path}': {e}")

        return ToolResult.failure(
            f"app '{app.name}' has no exe_path or protocol_uri configured",
            code=500,
        )
