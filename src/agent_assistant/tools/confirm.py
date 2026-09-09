"""B1/B2: Confirm gate — framework-level interception for dangerous operations.

Design:
- Tool base class exposes `requires_confirm` property (default False)
- Registry evaluates PermissionPolicy (allow/ask/deny); ASK → gate.confirm()
- ``requires_confirm`` is a fallback when no policy rule matches
- ConfirmGate holds a pluggable ConfirmProvider (CLI or UI)
- Fail-safe: no gate configured → deny; timeout → deny; EOF → deny
- Model CANNOT bypass this (confirm is not a tool parameter)
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ConfirmProvider(ABC):
    """Abstract confirm interaction provider."""

    @abstractmethod
    def confirm(self, tool_name: str, description: str) -> bool:
        """Ask user for confirmation. Returns True=approved, False=denied."""
        ...


class CLIConfirmProvider(ConfirmProvider):
    """Confirm via terminal stdin (blocking input prompt)."""

    def confirm(self, tool_name: str, description: str) -> bool:
        try:
            prompt = (
                f"\n⚠️  Confirm dangerous operation:\n"
                f"   Tool: {tool_name}\n"
                f"   Action: {description}\n"
                f"   Proceed? [y/N]: "
            )
            answer = input(prompt).strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            # Fail-safe: no stdin or interrupted → deny
            return False


class UIConfirmProvider(ConfirmProvider):
    """Confirm via UI (Dynamic Island confirmation button).

    Blocks the calling thread until UI responds or timeout.
    The UI bridge reads `pending_tool`/`pending_description` to render
    the confirm dialog, then calls `respond(bool)`.
    """

    def __init__(self, timeout: float = 60.0):
        self._timeout = timeout
        self._event = threading.Event()
        self._response = False
        self._pending_tool = ""
        self._pending_description = ""
        self._lock = threading.Lock()

    @property
    def pending_tool(self) -> str:
        return self._pending_tool

    @property
    def pending_description(self) -> str:
        return self._pending_description

    @property
    def has_pending(self) -> bool:
        return not self._event.is_set() and bool(self._pending_tool)

    def confirm(self, tool_name: str, description: str) -> bool:
        with self._lock:
            self._pending_tool = tool_name
            self._pending_description = description
            self._response = False
            self._event.clear()

        # Notify the frontend so it can show the confirm dialog.
        self._notify_ui(tool_name, description)

        # Block until UI responds or timeout
        responded = self._event.wait(timeout=self._timeout)

        with self._lock:
            self._pending_tool = ""
            self._pending_description = ""

        self._notify_ui_cleared()

        if not responded:
            logger.warning(
                "Confirm timeout for '%s' — denying (fail-safe)", tool_name
            )
            return False
        return self._response

    def respond(self, approved: bool) -> None:
        """Called by UI bridge when user clicks confirm/cancel."""
        self._response = approved
        self._event.set()

    @staticmethod
    def _notify_ui(tool_name: str, description: str) -> None:
        try:
            from agent_assistant.ui.bridge import api_bridge

            api_bridge.push_event(
                "confirm_request",
                {"tool": tool_name, "description": description},
            )
        except Exception as e:
            logger.debug("confirm_request push failed: %s", e)

    @staticmethod
    def _notify_ui_cleared() -> None:
        try:
            from agent_assistant.ui.bridge import api_bridge

            api_bridge.push_event("confirm_cleared", {})
        except Exception as e:
            logger.debug("confirm_cleared push failed: %s", e)


class ConfirmGate:
    """Central confirm gate used by the registry.

    Holds the active ConfirmProvider and exposes a simple confirm() API.
    """

    def __init__(self, provider: ConfirmProvider | None = None):
        self._provider = provider

    @property
    def provider(self) -> ConfirmProvider | None:
        return self._provider

    @provider.setter
    def provider(self, p: ConfirmProvider) -> None:
        self._provider = p

    def confirm(self, tool_name: str, description: str) -> bool:
        """Request user confirmation. Fail-safe: no provider → deny."""
        if self._provider is None:
            logger.warning(
                "No confirm provider configured — denying '%s'", tool_name
            )
            return False
        return self._provider.confirm(tool_name, description)


# Global singleton — configured at startup (main.py / launch.py)
confirm_gate = ConfirmGate()
