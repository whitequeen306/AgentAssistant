"""Tests for unified PermissionPolicy (allow / ask / deny)."""

from pathlib import Path
from unittest.mock import MagicMock

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.confirm import ConfirmGate
from agent_assistant.tools.permission import (
    PermissionAction,
    PermissionPolicy,
    PermissionRule,
    default_permission_policy,
    is_dangerous_command,
    path_in_dangerous_dir,
)
from agent_assistant.tools.registry import ToolRegistry
from agent_assistant.tools.run_command import RunCommandTool, is_dangerous


class EchoTool(Tool):
    def __init__(self, name: str = "echo"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo"

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(data=kwargs)


class AlwaysAskTool(Tool):
    @property
    def name(self) -> str:
        return "always_ask"

    @property
    def description(self) -> str:
        return "ask"

    @property
    def requires_confirm(self) -> bool:
        return True

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(data={"ok": True})


def test_is_dangerous_command_detects_rm():
    assert is_dangerous_command("Remove-Item foo")
    assert is_dangerous("rm -rf /tmp/x")
    assert not is_dangerous_command("Get-ChildItem")


def test_path_in_dangerous_dir():
    assert path_in_dangerous_dir(r"C:\Windows\System32\foo.dll")
    assert not path_in_dangerous_dir(str(Path.home() / "Documents" / "a.txt"))


def test_default_policy_asks_kill_and_save_note():
    policy = default_permission_policy()
    assert policy.evaluate("kill_process", {}).action is PermissionAction.ASK
    assert policy.evaluate("save_note", {"title": "x"}).action is PermissionAction.ASK


def test_default_policy_ui_automation():
    policy = default_permission_policy()
    assert policy.evaluate("ui_inspect", {}).action is PermissionAction.ALLOW
    assert policy.evaluate("ui_click", {"target": "确定"}).action is PermissionAction.ASK
    assert policy.evaluate("ui_type", {"text": "hi"}).action is PermissionAction.ASK
    assert policy.evaluate("ui_hotkey", {"keys": "enter"}).action is PermissionAction.ASK


def test_default_policy_allows_safe_run_command():
    policy = default_permission_policy()
    d = policy.evaluate("run_command", {"command": "Get-Date"})
    assert d.action is PermissionAction.ALLOW


def test_default_policy_asks_dangerous_run_command():
    policy = default_permission_policy()
    d = policy.evaluate("run_command", {"command": "Remove-Item C:\\temp\\x"})
    assert d.action is PermissionAction.ASK
    assert "dangerous" in d.reason


def test_default_policy_asks_move_to_system():
    policy = default_permission_policy()
    d = policy.evaluate(
        "move_file",
        {"src": r"C:\Users\a\b.txt", "dst": r"C:\Windows\b.txt"},
    )
    assert d.action is PermissionAction.ASK


def test_requires_confirm_fallback():
    policy = PermissionPolicy([])  # empty rules
    d = policy.evaluate("always_ask", {}, requires_confirm=True)
    assert d.action is PermissionAction.ASK


def test_deny_rule_blocks_without_confirm():
    policy = PermissionPolicy(
        [PermissionRule("echo", PermissionAction.DENY, reason="blocked")]
    )
    registry = ToolRegistry(permission_policy=policy)
    registry.register(EchoTool())
    provider = MagicMock()
    registry.confirm_gate = ConfirmGate(provider=provider)

    result = registry.execute("echo", {})
    assert result.ok is False
    assert result.code == 403
    assert result.error_category == "permission_denied"
    provider.confirm.assert_not_called()


def test_registry_asks_on_dangerous_run_command():
    registry = ToolRegistry()
    registry.register(RunCommandTool())
    calls: list[tuple[str, str]] = []

    class Rec:
        def confirm(self, tool_name: str, description: str) -> bool:
            calls.append((tool_name, description))
            return False

    registry.confirm_gate = ConfirmGate(provider=Rec())
    result = registry.execute(
        "run_command",
        {"command": "Remove-Item C:\\temp\\x -Recurse"},
    )
    assert result.ok is False
    assert result.error_category == "confirm_denied"
    assert len(calls) == 1
    assert calls[0][0] == "run_command"


def test_registry_allows_safe_run_command_without_confirm(monkeypatch):
    registry = ToolRegistry()
    tool = RunCommandTool()
    registry.register(tool)
    calls: list[str] = []

    class Rec:
        def confirm(self, tool_name: str, description: str) -> bool:
            calls.append(tool_name)
            return True

    registry.confirm_gate = ConfirmGate(provider=Rec())

    # Avoid actually spawning PowerShell
    monkeypatch.setattr(
        tool,
        "execute",
        lambda **kwargs: ToolResult.success(data={"exit_code": 0, "stdout": "ok"}),
    )
    result = registry.execute("run_command", {"command": "Write-Output hi"})
    assert result.ok is True
    assert calls == []


def test_requires_confirm_tool_still_gated():
    registry = ToolRegistry(permission_policy=PermissionPolicy([]))
    registry.register(AlwaysAskTool())

    class Rec:
        def confirm(self, tool_name: str, description: str) -> bool:
            return False

    registry.confirm_gate = ConfirmGate(provider=Rec())
    result = registry.execute("always_ask", {})
    assert result.ok is False
    assert result.code == 403
