"""Confirm provider for hosted mode: route dangerous-op confirmation to the
MCP client via an ``elicitation/create`` request.

In standalone mode the confirm gate is backed by ``UIConfirmProvider`` (its
own Dynamic-Island window). Under LianYu the engine has no window, so the
confirmation must bubble to the host's dialog. This provider blocks the
tool-execution thread until the client accepts/declines (fail-safe: any
timeout or transport error → deny).
"""

from __future__ import annotations

import logging

from agent_assistant.tools.confirm import ConfirmProvider
from agent_assistant.tools.labels import tool_label

from .stdio_connection import StdioConnection

logger = logging.getLogger(__name__)


class BridgeConfirmProvider(ConfirmProvider):
    """Ask the MCP host to confirm a dangerous tool via elicitation."""

    def __init__(self, connection: StdioConnection, timeout: float = 60.0) -> None:
        self._conn = connection
        self._timeout = timeout

    def confirm(self, tool_name: str, description: str) -> bool:
        # 用户看中文动作名（「运行命令」），不看内部工具名（run_command）。
        message = (
            f"角色想在你的电脑上「{tool_label(tool_name)}」\n"
            f"{description}\n是否允许？"
        )
        result = self._conn.server_request(
            "elicitation/create",
            {
                "message": message,
                # No fields to collect — this is a pure accept/decline gate.
                "requestedSchema": {"type": "object", "properties": {}},
            },
            self._timeout,
        )
        approved = bool(result) and result.get("action") == "accept"
        logger.info(
            "hosted confirm: tool=%s approved=%s", tool_name, approved
        )
        return approved
