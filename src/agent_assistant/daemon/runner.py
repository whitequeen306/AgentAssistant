"""Daemon runner — orchestrates all background triggers.

Starts/stops all background components and wires their events
to the main agent's event handler.

The daemon does NOT run an LLM. It only:
1. Runs lightweight monitors/triggers
2. Emits events when something fires
3. The event handler wakes the main agent to process
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_assistant.daemon.context_menu import (
    is_registered,
    learn_is_registered,
    register_context_menu,
    register_learn_context_menu,
)
from agent_assistant.daemon.events import EventType, TriggerEvent, event_bus
from agent_assistant.daemon.morning_briefing import (
    BriefingConfig,
    MorningBriefingTrigger,
)
from agent_assistant.daemon.perf_monitor import PerfMonitor

if TYPE_CHECKING:
    from agent_assistant.agent.loop import AgentLoop

logger = logging.getLogger(__name__)


def _setting(key: str, default: str = "") -> str:
    """Read a UI setting; fall back to default when the store is unavailable
    (CLI mode, tests without a DB, etc.)."""
    try:
        from agent_assistant.ui.store import ui_store

        return ui_store.get_setting(key, default)
    except Exception:
        return default


class DaemonRunner:
    """Orchestrates background triggers and connects them to the agent."""

    def __init__(self, agent: AgentLoop | None = None, bridge=None) -> None:
        # ``agent`` is CLI/legacy fallback. UI mode passes ``bridge`` and routes
        # through AgentPool (per-conversation loops) via bridge helpers.
        self._agent = agent
        self._bridge = bridge
        self._perf_monitor = PerfMonitor()
        self._morning_briefing = MorningBriefingTrigger(
            BriefingConfig(
                enabled=_setting("feature_briefing", "on") == "on",
                window_start=_setting("briefing_window_start", "06:30") or "06:30",
                window_end=_setting("briefing_window_end", "11:30") or "11:30",
            )
        )
        self._perf_enabled = _setting("feature_perf_monitor", "on") == "on"
        self._running = False

    def set_agent(self, agent: AgentLoop) -> None:
        """Set a fallback single agent (CLI). Prefer bridge + AgentPool in UI."""
        self._agent = agent

    def start(self) -> None:
        """Start all background components."""
        if self._running:
            return
        self._running = True

        # Subscribe agent handler to event bus
        event_bus.subscribe(self._handle_event)
        event_bus.start()

        # Start triggers (respecting 功能开关 toggles)
        if self._perf_enabled:
            self._perf_monitor.start()
        else:
            logger.info("Perf monitor disabled (feature_perf_monitor=off)")
        self._morning_briefing.start()  # self-skips when feature_briefing=off

        # Register context menus if not already (idempotent, HKCU)
        if not is_registered():
            if register_context_menu():
                logger.info("Right-click context menu registered")
            else:
                logger.warning("Failed to register context menu")
        if not learn_is_registered():
            if register_learn_context_menu():
                logger.info("Learn-with-Assistant context menu registered")
            else:
                logger.warning("Failed to register Learn context menu")

        logger.info("Daemon started (perf_monitor + morning_briefing + context_menu)")

    def stop(self) -> None:
        """Stop all background components."""
        self._running = False
        self._perf_monitor.stop()
        self._morning_briefing.stop()
        event_bus.stop()
        logger.info("Daemon stopped")

    def _chat(self, message: str) -> str:
        """Route a daemon-initiated turn to bridge (pool) or legacy agent."""
        if self._bridge is not None:
            return self._bridge.chat_in_conversation(message)
        if self._agent is not None:
            return self._agent.chat(message)
        logger.warning("No bridge/agent — dropping daemon chat")
        return ""

    def _handle_event(self, event: TriggerEvent) -> None:
        """Route trigger events to the main agent."""
        if not self._bridge and not self._agent:
            logger.warning("No bridge/agent set — dropping event: %s", event.type)
            return

        logger.info("Daemon handling event: %s", event.type.value)

        if event.type == EventType.PERF_ANOMALY:
            message = event.payload.get("message", "Performance anomaly detected.")
            if self._bridge:
                # Dedicated 「性能检测」 conversation + its own AgentLoop.
                self._bridge.report_perf(message)
            else:
                self._agent.chat(message)

        elif event.type == EventType.MORNING_BRIEFING:
            # E1: Full closed loop — research + notify with actions
            self._handle_morning_briefing(event)

        elif event.type == EventType.RIGHT_CLICK:
            # E2: UI picker (translate/summarize/ask), not LLM free-form
            self._handle_right_click(event)

        elif event.type == EventType.VOICE_COMMAND:
            text = event.payload.get("text", "")
            if text:
                self._chat(text)

        else:
            logger.warning("Unhandled event type: %s", event.type)

    def _handle_morning_briefing(self, event: TriggerEvent) -> None:
        """E1: Morning briefing — runs silently in its own pinned conversation.

        Delivery mirrors 性能检测: the research runs in the dedicated 「晨间播报」
        conversation (own AgentLoop, stream suppressed), then a clickable system
        line is pushed into the current chat. Follow-up questions happen inside
        that conversation, which already holds the full report context.
        """
        goal = event.payload.get("goal", "")

        message = (
            f"[Morning Briefing Triggered]\n"
            f"Please dispatch the research sub-agent with this goal:\n{goal}\n\n"
            f"After the report is ready:\n"
            f"1. Save it as a note\n"
            f"2. Reply in 简体中文 with a short index: 3 new terms + 3 GitHub repos "
            f"(name + one-line why). Do not summarize 'yesterday's AI news'."
        )
        if self._bridge:
            # Dedicated 「晨间播报」 conversation + its own AgentLoop — same
            # pattern as 性能检测: never hijacks the user's active chat window.
            self._bridge.report_briefing(message)
        else:
            self._chat(message)

    def _handle_right_click(self, event: TriggerEvent) -> None:
        """E2: Right-click shows UI picker, not LLM free-form question."""
        text = event.payload.get("text", "")
        if not text:
            return
        if not self._bridge:
            logger.warning("Right-click text ignored — no UI bridge")
            return
        self._bridge.push_event("right_click_picker", {"text": text[:500]})
