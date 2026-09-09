"""Session & notification tools: create_session, notify.

Per docs/06-tool-spec.md §1.15–1.16.
J3/F4: notify supports actions (buttons) with callback routing.
J4: create_session switches the main agent to the new session.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)


# ─── Session Management ────────────────────────────────────────────────────────

@dataclass
class Session:
    """A conversation session with injected context."""

    id: str
    topic: str
    context: str
    history: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """Manages conversation sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._active_session_id: str | None = None
        # Callback to switch the main agent's context
        self._on_switch: Callable[[Session], None] | None = None

    def set_switch_callback(self, callback: Callable[[Session], None]) -> None:
        """Set callback invoked when session switches (J4)."""
        self._on_switch = callback

    def create(self, context: str, topic: str = "") -> Session:
        session_id = uuid.uuid4().hex[:8]
        session = Session(
            id=session_id, topic=topic or f"session-{session_id}", context=context
        )
        self._sessions[session_id] = session
        return session

    def switch_to(self, session_id: str) -> Session | None:
        """J4: Switch active session and notify the agent loop."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        self._active_session_id = session_id
        if self._on_switch:
            self._on_switch(session)
        logger.info("Switched to session: %s (%s)", session_id, session.topic)
        return session

    @property
    def active_session(self) -> Session | None:
        if self._active_session_id:
            return self._sessions.get(self._active_session_id)
        return None

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())


# Global session manager
session_manager = SessionManager()


class CreateSessionTool(Tool):
    @property
    def name(self) -> str:
        return "create_session"

    @property
    def description(self) -> str:
        return (
            "Create a new conversation session and inject initial context. "
            "When to call: morning briefing 'ask about this report' "
            "(injects scenario + full report), starting a new topic, "
            "user says 'open a new topic'. Returns a new session id. "
            "When NOT to call: continuing in the current session."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="context",
                type="string",
                description="System context to inject (scenario + background)",
            ),
            ToolParameter(
                name="topic",
                type="string",
                description="Topic label (optional)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        context: str = kwargs.get("context", "").strip()
        topic: str = kwargs.get("topic", "").strip()

        if not context:
            return ToolResult.failure("parameter 'context' is required")

        session = session_manager.create(context=context, topic=topic)

        # J4: Switch the agent to the new session
        session_manager.switch_to(session.id)

        return ToolResult.success(data={
            "session_id": session.id,
            "topic": session.topic,
            "message": (
                f"Session created and activated. "
                f"Context injected ({len(context)} chars). "
                f"You are now in this session."
            ),
        })


# ─── Windows Toast Notification (J3/F4: with actions support) ────────────────

# Action callback registry: action_id → callable
_action_callbacks: dict[str, Callable[[], None]] = {}


def register_action_callback(action_id: str, callback: Callable[[], None]) -> None:
    """Register a callback for a toast action button (F4)."""
    _action_callbacks[action_id] = callback


def invoke_action_callback(action_id: str) -> bool:
    """Invoke a registered action callback. Returns True if found."""
    cb = _action_callbacks.get(action_id)
    if cb:
        cb()
        return True
    return False


def _show_toast(
    title: str, body: str, actions: list[dict[str, str]] | None = None
) -> bool:
    """Show a Windows toast notification via PowerShell.

    Args:
        title: Notification title.
        body: Notification body text.
        actions: Optional list of {"id": "...", "label": "..."} buttons.
    """
    # Escape XML special chars
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Build actions XML
    actions_xml = ""
    if actions:
        btns = "".join(
            f'<action content="{esc(a["label"])}" '
            f'arguments="action={esc(a["id"])}" />'
            for a in actions
        )
        actions_xml = f"<actions>{btns}</actions>"

    xml_content = f"""
<toast>
    <visual>
        <binding template="ToastGeneric">
            <text>{esc(title)}</text>
            <text>{esc(body)}</text>
        </binding>
    </visual>
    {actions_xml}
</toast>
"""

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$xml.LoadXml(@'\n"
        f"{xml_content.strip()}\n"
        "'@)\n"
        "$toast = [Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('AgentAssistant')\n"
        "$notification = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
        "$toast.Show($notification)"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


class NotifyTool(Tool):
    @property
    def name(self) -> str:
        return "notify"

    @property
    def description(self) -> str:
        return (
            "Pop a Windows toast notification with optional action buttons. "
            "When to call: morning briefing popup (summary + buttons), "
            "long-task completion reminders. "
            "Actions: list of {id, label} buttons shown on the toast. "
            "When NOT to call: for simple alerts in voice mode use TTS."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="title", type="string", description="Notification title"),
            ToolParameter(name="body", type="string", description="Notification body text"),
            ToolParameter(
                name="actions",
                type="string",
                description=(
                    'JSON array of action buttons: [{"id":"preview","label":"Preview"}]'
                ),
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        title: str = kwargs.get("title", "").strip()
        body: str = kwargs.get("body", "").strip()
        actions_raw: str = kwargs.get("actions", "").strip()

        if not title or not body:
            return ToolResult.failure("both 'title' and 'body' are required")

        # Parse actions JSON
        actions: list[dict[str, str]] | None = None
        if actions_raw:
            try:
                actions = json.loads(actions_raw)
            except (json.JSONDecodeError, TypeError):
                return ToolResult.failure(
                    "'actions' must be valid JSON array of {id, label} objects"
                )

        success = _show_toast(title, body, actions=actions)
        if success:
            return ToolResult.success(data={
                "shown": True,
                "title": title,
                "actions": [a["id"] for a in actions] if actions else [],
            })
        else:
            return ToolResult.failure("failed to show toast notification", code=500)
