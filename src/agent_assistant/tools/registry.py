"""Tool registry — registers tools and generates OpenAI schemas.

The registry is the single source of truth for which tools the agent has.
The agent loop asks the registry for schemas to pass to the LLM, and
dispatches tool calls back through the registry.

Before execute, a PermissionPolicy decides allow / ask / deny.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_assistant.tools.base import Tool, ToolResult
from agent_assistant.tools.confirm import ConfirmGate
from agent_assistant.tools.confirm import confirm_gate as global_confirm_gate
from agent_assistant.tools.permission import (
    PermissionAction,
    PermissionPolicy,
    UserOverridePolicy,
    default_permission_policy,
)
from agent_assistant.tools.sanitize import sanitize_error

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Holds all registered tools and dispatches calls."""

    def __init__(
        self,
        permission_policy: PermissionPolicy | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        # Use global confirm gate by default; overridable for testing
        self.confirm_gate: ConfirmGate | None = global_confirm_gate
        self.permission_policy = permission_policy or default_permission_policy()

    def register(self, tool: Tool) -> None:
        """Register a tool instance."""
        if tool.name in self._tools:
            logger.warning("Tool '%s' already registered, overwriting.", tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool from the registry (no-op when absent).

        Used by profile-specific registration (e.g. hosted mode) to strip
        tools that make no sense in that runtime.
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Generate the tools list for OpenAI chat completion API."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolResult:
        """Dispatch a tool call by name.

        `arguments` may be a JSON string (from the model) or a dict.
        Never raises — returns ToolResult.failure() on any error.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.failure(f"unknown tool: {name}", code=404)

        # Parse arguments
        if isinstance(arguments, str):
            try:
                kwargs = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return ToolResult.failure(f"invalid JSON arguments: {e}", code=400)
        else:
            kwargs = arguments

        decision = self.permission_policy.evaluate(
            name,
            kwargs if isinstance(kwargs, dict) else {},
            requires_confirm=tool.requires_confirm,
        )

        if decision.action is PermissionAction.DENY:
            return ToolResult.failure(
                f"Permission denied for '{name}'"
                + (f" ({decision.reason})" if decision.reason else "")
                + ". Do not retry without changing approach.",
                code=403,
                error_category="permission_denied",
            )

        if decision.action is PermissionAction.ASK:
            if self.confirm_gate is None:
                return ToolResult.failure(
                    f"'{name}' requires user confirmation but no confirm "
                    f"provider is configured. Operation denied.",
                    code=403,
                    error_category="confirm_denied",
                )
            confirm_desc = _confirm_description(name, kwargs, decision.reason)
            approved = self.confirm_gate.confirm(
                tool_name=name,
                description=confirm_desc,
            )
            if not approved:
                return ToolResult.failure(
                    f"User denied confirmation for '{name}'. "
                    f"Operation cancelled. Do not retry without user consent.",
                    code=403,
                    error_category="confirm_denied",
                )

        # Resource guard: desktop / filesystem_write single-ownership. A
        # subagent that owns the resource passes; the main agent acquires
        # ephemerally; anyone else gets a clean resource_busy failure.
        from agent_assistant.subagents.context import current_execution_context
        from agent_assistant.subagents.resources import ResourceBusyError, resource_coordinator

        context = current_execution_context()
        owner_id = context.owner_id if context is not None else None
        try:
            with resource_coordinator.guard_tool(name, owner_id=owner_id):
                result = tool.execute(**kwargs)
            logger.info("Tool '%s' → ok=%s", name, result.ok)
            return result
        except ResourceBusyError:
            return ToolResult.failure(
                f"'{name}' is unavailable right now: another task is "
                "controlling this resource (desktop or file writes). Wait for "
                "it to finish or ask the user to stop it.",
                code=423,
                error_category="resource_busy",
            )
        except Exception as e:
            # B9: Error sanitization — log full detail, return safe category message.
            # Agent loop will also attach feedback.trace (turn_failures) for the model.
            logger.exception("Tool '%s' raised unexpectedly", name)
            sanitized = sanitize_error(str(e), context=name, code=500)
            return ToolResult.failure(
                sanitized.safe_message,
                code=500,
                error_category=sanitized.category,
            )


def _confirm_description(
    name: str,
    kwargs: dict[str, Any],
    reason: str = "",
) -> str:
    """Human-readable blurb for the UI confirm dialog.

    Chinese-first: the model-provided ``purpose`` (为什么要做) leads; raw
    technical detail (command text, paths) stays visible but secondary so an
    informed user can verify the model isn't claiming one thing and doing
    another.
    """
    purpose = str(kwargs.get("purpose") or "").strip()[:120]
    if name == "save_note":
        title = str(kwargs.get("title") or "").strip() or "（无标题）"
        tag = str(kwargs.get("tag") or "").strip()
        content = str(kwargs.get("content") or "")
        preview = content.strip().replace("\n", " ")
        if len(preview) > 160:
            preview = preview[:159] + "…"
        lines = [f"保存笔记「{title}」到本地资料库。"]
        if tag:
            lines.append(f"标签：{tag}")
        if preview:
            lines.append(f"预览：{preview}")
        return "\n".join(lines)
    if name == "kill_process":
        pid = kwargs.get("pid")
        pname = kwargs.get("name")
        target = f"PID {pid}" if pid is not None else str(pname or "")
        lines = [f"结束进程「{target}」。"]
        if purpose:
            lines.insert(0, f"目的：{purpose}")
        lines.append("误杀可能导致程序退出、未保存内容丢失。")
        return "\n".join(lines)
    if name == "run_command":
        # 用户只看中文目的说明；仅当模型没提供 purpose 时才退回显示命令原文
        # （否则弹窗为空，用户无从判断）。
        if purpose:
            return f"目的：{purpose}"
        cmd = str(kwargs.get("command") or "").strip()
        if len(cmd) > 160:
            cmd = cmd[:159] + "…"
        return f"命令：{cmd}"
    if name == "move_file":
        return (
            f"移动文件到受保护目录。\n"
            f"源：{kwargs.get('src', '')}\n"
            f"目标：{kwargs.get('dst', '')}"
        )
    if name in ("write_file", "edit_file"):
        return f"写入受保护系统路径：{kwargs.get('path', '')}"
    # Generic fallback — keep args short, never dump secrets wholesale.
    from agent_assistant.tools.labels import tool_label

    try:
        raw = json.dumps(
            {k: v for k, v in kwargs.items() if k != "purpose"},
            ensure_ascii=False,
        )
    except Exception:
        raw = str(kwargs)
    if len(raw) > 240:
        raw = raw[:239] + "…"
    lines = [f"执行「{tool_label(name)}」"]
    if purpose:
        lines.append(f"目的：{purpose}")
    if reason:
        lines.append(f"原因：{_reason_zh(reason)}")
    lines.append(f"参数：{raw}")
    return "\n".join(lines)


# Permission-rule reasons are English identifiers; the confirm dialog is
# user-facing Chinese.
_REASON_ZH: dict[str, str] = {
    "dangerous command": "命中危险命令规则",
    "terminate process": "结束进程",
    "persist note to disk": "写入本地笔记",
    "click desktop UI": "点击桌面界面",
    "type into desktop UI": "向桌面界面输入",
    "send keys to desktop UI": "向桌面界面发送按键",
    "scroll desktop UI": "滚动桌面界面",
    "destination in system directory": "目标在系统目录",
    "path in system directory": "路径在系统目录",
    "tool.requires_confirm": "该工具默认需要确认",
    "user override: ask": "你在设置中要求询问",
}


def _reason_zh(reason: str) -> str:
    return _REASON_ZH.get(reason, reason)


# Global singleton registry — user per-tool overrides (settings page) included
tool_registry = ToolRegistry(permission_policy=UserOverridePolicy())
