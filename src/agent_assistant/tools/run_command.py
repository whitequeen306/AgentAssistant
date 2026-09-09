"""run_command tool — Execute a command in PowerShell.

Dangerous commands are gated by PermissionPolicy (ask) in the registry —
this module only executes.
"""

from __future__ import annotations

import subprocess
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.permission import (
    DANGEROUS_COMMAND_PATTERNS,
    is_dangerous_command,
)
from agent_assistant.tools.sanitize import sanitize_error

# Backward-compatible aliases for tests / callers
DANGEROUS_PATTERNS = DANGEROUS_COMMAND_PATTERNS


def is_dangerous(command: str) -> bool:
    """Check if a command matches dangerous patterns."""
    return is_dangerous_command(command)


class RunCommandTool(Tool):
    @property
    def name(self) -> str:
        return "run_command"

    @property
    def description(self) -> str:
        return (
            "Execute a command in a PowerShell terminal. "
            "When to call: install software, run scripts, git operations, system queries. "
            "Has a whitelist: dangerous commands (delete/format/registry edits/process kills) "
            "require user confirmation (framework permission policy). "
            "ALWAYS pass `purpose`: one plain-Chinese sentence saying what the command does "
            "and why — it is shown verbatim in the user's confirm dialog (users cannot read "
            "raw PowerShell). "
            "When NOT to call: reading files → use read_file; launching apps → use launch_app; "
            "grabbing selection → use get_selection. Default timeout 120s."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description="PowerShell command to execute",
            ),
            ToolParameter(
                name="purpose",
                type="string",
                description=(
                    "一句中文说明这条命令要做什么、为什么需要"
                    "（原样显示在用户确认弹窗里，例：查看网易云音乐是否正在运行）"
                ),
                required=False,
            ),
            ToolParameter(
                name="workdir",
                type="string",
                description="Working directory (optional)",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="integer",
                description="Timeout in milliseconds (default 120000)",
                required=False,
                default=120000,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        command: str = kwargs.get("command", "").strip()
        workdir: str | None = kwargs.get("workdir")
        timeout_ms: int = kwargs.get("timeout", 120000)

        if not command:
            return ToolResult.failure("parameter 'command' is required")

        # Confirm for dangerous commands is handled by PermissionPolicy in registry.

        timeout_s = max(1, timeout_ms // 1000)

        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                encoding="utf-8",
                errors="replace",
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode == 0:
                return ToolResult.success(
                    data={
                        "exit_code": 0,
                        "stdout": stdout[:10000],  # Limit output size
                        "stderr": stderr[:2000] if stderr else None,
                    }
                )
            else:
                # B9: sanitize stderr before model sees it
                sanitized = sanitize_error(
                    stderr[:2000] if stderr else f"exit code {result.returncode}",
                    context="run_command",
                    code=result.returncode,
                )
                return ToolResult.failure(
                    f"Command exited with code {result.returncode}. {sanitized.safe_message}",
                    code=result.returncode,
                )

        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                f"command timed out after {timeout_s}s",
                code=408,
            )
        except FileNotFoundError:
            return ToolResult.failure("powershell not found on this system", code=500)
        except Exception as e:
            sanitized = sanitize_error(str(e), context="run_command", code=500)
            return ToolResult.failure(sanitized.safe_message, code=500)
