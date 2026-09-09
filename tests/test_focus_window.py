"""focus_window / focus_window_hwnd — no WINDOWPLACEMENT dependency."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent_assistant.tools.permission import PermissionAction, default_permission_policy
from agent_assistant.win_utils import WindowInfo


def test_focus_window_hwnd_uses_is_iconic_not_placement():
    from agent_assistant import win_utils

    mock_user32 = MagicMock()
    mock_user32.IsIconic.return_value = 1  # minimized
    with patch.object(win_utils, "user32", mock_user32):
        assert win_utils.focus_window_hwnd(12345) is True

    mock_user32.IsIconic.assert_called_once_with(12345)
    mock_user32.ShowWindow.assert_called_once()
    mock_user32.SetForegroundWindow.assert_called_once_with(12345)
    assert mock_user32.SetWindowPos.call_count == 2


def test_focus_window_tool_returns_failure_not_raise():
    from agent_assistant.tools.focus_window import FocusWindowTool

    win = WindowInfo(hwnd=1, title="云音乐", pid=99)
    with patch(
        "agent_assistant.tools.focus_window.find_windows", return_value=[win]
    ):
        with patch(
            "agent_assistant.tools.focus_window.focus_window_hwnd",
            side_effect=RuntimeError("boom"),
        ):
            result = FocusWindowTool().execute(title_pattern="云音乐")

    assert result.ok is False
    assert result.code == 500
    # Sanitized — no raw "boom" / traceback for the model
    assert "boom" not in (result.error or "").lower()


def test_registry_unexpected_raise_keeps_error_category():
    from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
    from agent_assistant.tools.registry import ToolRegistry

    class BoomTool(Tool):
        @property
        def name(self) -> str:
            return "boom_tool"

        @property
        def description(self) -> str:
            return "test"

        @property
        def parameters(self) -> list[ToolParameter]:
            return []

        def execute(self, **kwargs) -> ToolResult:
            raise AttributeError("module has no attribute 'WINDOWPLACEMENT'")

    reg = ToolRegistry(permission_policy=default_permission_policy())
    reg.register(BoomTool())
    result = reg.execute("boom_tool", "{}")
    assert result.ok is False
    assert result.error_category  # set for turn_trace / feedback
    assert result.code == 500


def test_focus_window_exe_fallback_when_title_is_song():
    """NetEase HWND title is the track; product-name pattern must still focus."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_assistant.tools.focus_window import FocusWindowTool
    from agent_assistant.win_utils import WindowInfo

    running = WindowInfo(hwnd=5, title="大海 - 张雨生", pid=28952)
    app = SimpleNamespace(exe_path=r"D:\CloudMusic\cloudmusic.exe")
    with patch("agent_assistant.tools.focus_window.find_windows", return_value=[]):
        with patch(
            "agent_assistant.tools.focus_window.find_windows_by_exe",
            return_value=[running],
        ):
            with patch(
                "agent_assistant.app_registry.app_registry.find_app",
                return_value=app,
            ):
                with patch(
                    "agent_assistant.tools.focus_window.focus_window_hwnd",
                    return_value=True,
                ):
                    result = FocusWindowTool().execute(title_pattern="网易云音乐")
    assert result.ok
    assert result.data["focused"] == "大海 - 张雨生"


def test_launch_app_focuses_by_exe_when_title_mismatch():
    from types import SimpleNamespace
    from unittest.mock import patch

    from agent_assistant.tools.launch_app import LaunchAppTool
    from agent_assistant.win_utils import WindowInfo

    app = SimpleNamespace(
        name="网易云音乐",
        exe_path=r"D:\CloudMusic\cloudmusic.exe",
        protocol_uri=None,
        window_title_pattern="网易云音乐",
        workdir=None,
    )
    running = WindowInfo(hwnd=5, title="大海 - 张雨生", pid=28952)
    with patch(
        "agent_assistant.tools.launch_app.app_registry.discover_app",
        return_value=app,
    ):
        with patch("agent_assistant.tools.launch_app.find_windows", return_value=[]):
            with patch(
                "agent_assistant.tools.launch_app.find_windows_by_exe",
                return_value=[running],
            ):
                with patch(
                    "agent_assistant.tools.launch_app.focus_window_hwnd",
                    return_value=True,
                ):
                    result = LaunchAppTool().execute(name="网易云音乐")
    assert result.ok
    assert result.data["action"] == "focused"
    assert result.data["window_title"] == "大海 - 张雨生"


def test_focus_window_default_allow():
    policy = default_permission_policy()
    assert policy.evaluate("focus_window", {"title_pattern": "x"}).action is PermissionAction.ALLOW
