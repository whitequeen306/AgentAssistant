"""Tests for B1/B2: Confirm gate framework.

Covers:
- Tool.requires_confirm property (default False)
- Registry blocks execution when confirm denied
- Registry proceeds when confirm approved
- CLI confirm provider (stdin mock)
- UI confirm provider (event-based mock)
- Conditional confirm: run_command dangerous triggers gate
- kill_process always requires confirm
- Confirm denial returns structured ToolResult
- Idempotency: safe tools never trigger confirm
"""

import threading
from unittest.mock import patch

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.confirm import (
    CLIConfirmProvider,
    ConfirmGate,
    ConfirmProvider,
    UIConfirmProvider,
)
from agent_assistant.tools.registry import ToolRegistry

# ─── Test fixtures ────────────────────────────────────────────────────────────


class SafeTool(Tool):
    """A tool that never requires confirm."""

    @property
    def name(self) -> str:
        return "safe_tool"

    @property
    def description(self) -> str:
        return "A safe tool"

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(data={"done": True})


class DangerousTool(Tool):
    """A tool that always requires confirm."""

    @property
    def name(self) -> str:
        return "dangerous_tool"

    @property
    def description(self) -> str:
        return "A dangerous tool"

    @property
    def parameters(self) -> list[ToolParameter]:
        return []

    @property
    def requires_confirm(self) -> bool:
        return True

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult.success(data={"killed": True})


class AutoApproveProvider(ConfirmProvider):
    """Test provider that always approves."""

    def confirm(self, tool_name: str, description: str) -> bool:
        return True


class AutoDenyProvider(ConfirmProvider):
    """Test provider that always denies."""

    def confirm(self, tool_name: str, description: str) -> bool:
        return False


class RecordingProvider(ConfirmProvider):
    """Records what was asked, configurable response."""

    def __init__(self, response: bool = True):
        self.calls: list[tuple[str, str]] = []
        self._response = response

    def confirm(self, tool_name: str, description: str) -> bool:
        self.calls.append((tool_name, description))
        return self._response


# ─── Tests: Tool.requires_confirm property ────────────────────────────────────


class TestToolConfirmProperty:
    def test_default_is_false(self):
        tool = SafeTool()
        assert tool.requires_confirm is False

    def test_override_to_true(self):
        tool = DangerousTool()
        assert tool.requires_confirm is True


# ─── Tests: Registry confirm interception ─────────────────────────────────────


class TestRegistryConfirmGate:
    def test_safe_tool_no_confirm_needed(self):
        """Safe tools execute without any confirm check."""
        registry = ToolRegistry()
        registry.register(SafeTool())
        provider = RecordingProvider()
        gate = ConfirmGate(provider=provider)
        registry.confirm_gate = gate

        result = registry.execute("safe_tool", {})
        assert result.ok is True
        assert len(provider.calls) == 0  # Never asked

    def test_dangerous_tool_approved(self):
        """Dangerous tool executes when user approves."""
        registry = ToolRegistry()
        registry.register(DangerousTool())
        gate = ConfirmGate(provider=AutoApproveProvider())
        registry.confirm_gate = gate

        result = registry.execute("dangerous_tool", {})
        assert result.ok is True
        assert result.data == {"killed": True}

    def test_dangerous_tool_denied(self):
        """Dangerous tool blocked when user denies."""
        registry = ToolRegistry()
        registry.register(DangerousTool())
        gate = ConfirmGate(provider=AutoDenyProvider())
        registry.confirm_gate = gate

        result = registry.execute("dangerous_tool", {})
        assert result.ok is False
        assert result.code == 403
        assert "denied" in result.error.lower() or "confirm" in result.error.lower()

    def test_dangerous_tool_no_gate_configured(self):
        """Without a gate, dangerous tool is blocked (fail-safe)."""
        registry = ToolRegistry()
        registry.register(DangerousTool())
        # No confirm_gate set

        result = registry.execute("dangerous_tool", {})
        assert result.ok is False
        assert result.code == 403

    def test_confirm_receives_tool_info(self):
        """Provider gets tool name and description for display."""
        registry = ToolRegistry()
        registry.register(DangerousTool())
        provider = RecordingProvider(response=True)
        gate = ConfirmGate(provider=provider)
        registry.confirm_gate = gate

        registry.execute("dangerous_tool", {})
        assert len(provider.calls) == 1
        name, desc = provider.calls[0]
        assert name == "dangerous_tool"
        assert "dangerous" in desc.lower()


# ─── Tests: CLI confirm provider ──────────────────────────────────────────────


class TestCLIConfirmProvider:
    def test_approve_on_y(self):
        provider = CLIConfirmProvider()
        with patch("builtins.input", return_value="y"):
            assert provider.confirm("kill_process", "Kill chrome.exe") is True

    def test_approve_on_yes(self):
        provider = CLIConfirmProvider()
        with patch("builtins.input", return_value="yes"):
            assert provider.confirm("kill_process", "Kill chrome.exe") is True

    def test_deny_on_n(self):
        provider = CLIConfirmProvider()
        with patch("builtins.input", return_value="n"):
            assert provider.confirm("kill_process", "Kill chrome.exe") is False

    def test_deny_on_empty(self):
        provider = CLIConfirmProvider()
        with patch("builtins.input", return_value=""):
            assert provider.confirm("kill_process", "Kill chrome.exe") is False

    def test_deny_on_eof(self):
        """EOFError (no stdin) → deny (fail-safe)."""
        provider = CLIConfirmProvider()
        with patch("builtins.input", side_effect=EOFError):
            assert provider.confirm("kill_process", "Kill chrome.exe") is False


# ─── Tests: UI confirm provider ───────────────────────────────────────────────


class TestUIConfirmProvider:
    def test_approve_via_event(self):
        provider = UIConfirmProvider()
        # Simulate UI responding in background
        def respond():
            import time
            time.sleep(0.05)
            provider.respond(True)

        t = threading.Thread(target=respond)
        t.start()
        assert provider.confirm("kill_process", "Kill chrome.exe") is True
        t.join()

    def test_deny_via_event(self):
        provider = UIConfirmProvider()
        def respond():
            import time
            time.sleep(0.05)
            provider.respond(False)

        t = threading.Thread(target=respond)
        t.start()
        assert provider.confirm("kill_process", "Kill chrome.exe") is False
        t.join()

    def test_timeout_denies(self):
        """If UI doesn't respond within timeout, deny (fail-safe)."""
        provider = UIConfirmProvider(timeout=0.1)
        # No response → timeout → deny
        assert provider.confirm("kill_process", "Kill chrome.exe") is False

    def test_pending_request_info(self):
        """UI provider exposes pending request for bridge to read."""
        provider = UIConfirmProvider(timeout=0.1)

        def check_pending():
            import time
            time.sleep(0.02)
            assert provider.pending_tool == "kill_process"
            assert "chrome" in provider.pending_description
            provider.respond(True)

        t = threading.Thread(target=check_pending)
        t.start()
        provider.confirm("kill_process", "Kill chrome.exe")
        t.join()


# ── Confirm dialog copy (user-facing Chinese) ────────────────────────────────


class TestConfirmDescriptionCopy:
    """The confirm dialog is read by end users: Chinese first, raw command
    kept (truncated) so informed users can verify the model's claim."""

    def test_run_command_with_purpose_shows_only_purpose(self):
        from agent_assistant.tools.registry import _confirm_description

        desc = _confirm_description(
            "run_command",
            {
                "command": 'Get-Process -Name "cloudmusic*" | Format-List',
                "purpose": "查看网易云音乐是否正在运行",
            },
            reason="dangerous command",
        )
        assert desc == "目的：查看网易云音乐是否正在运行"

    def test_run_command_without_purpose_falls_back_to_command(self):
        from agent_assistant.tools.registry import _confirm_description

        desc = _confirm_description("run_command", {"command": "dir"})
        assert desc == "命令：dir"

    def test_run_command_fallback_truncates_long_command(self):
        from agent_assistant.tools.registry import _confirm_description

        desc = _confirm_description("run_command", {"command": "x" * 500})
        assert desc.startswith("命令：")
        assert len(desc) < 200
        assert desc.endswith("…")

    def test_kill_process_purpose_first(self):
        from agent_assistant.tools.registry import _confirm_description

        desc = _confirm_description(
            "kill_process",
            {"name": "cloudmusic.exe", "purpose": "网易云卡死需要重启"},
        )
        lines = desc.splitlines()
        assert lines[0] == "目的：网易云卡死需要重启"
        assert "cloudmusic.exe" in lines[1]

    def test_bridge_confirm_uses_chinese_label(self):
        from unittest.mock import MagicMock

        from agent_assistant.hosted.bridge_confirm import BridgeConfirmProvider

        conn = MagicMock()
        conn.server_request.return_value = {"action": "accept"}
        provider = BridgeConfirmProvider(conn, timeout=1.0)
        assert provider.confirm("run_command", "目的：测试") is True
        message = conn.server_request.call_args[0][1]["message"]
        assert "「运行命令」" in message
        assert "run_command" not in message
