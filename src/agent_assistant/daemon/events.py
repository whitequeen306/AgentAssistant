"""Event bus — connects background triggers to the main agent.

Background triggers (perf monitor, morning briefing, hotkey) emit events.
The daemon subscribes the main agent's handler to process them.

This is the "trigger → wake agent" bridge described in the architecture.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(Enum):
    RIGHT_CLICK = "right_click"
    VOICE_COMMAND = "voice_command"
    HOTKEY = "hotkey"


@dataclass
class TriggerEvent:
    """An event emitted by a background trigger."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # which trigger emitted this


# Type alias for event handlers
EventHandler = Callable[[TriggerEvent], None]


class EventBus:
    """Simple thread-safe event bus for trigger → agent communication."""

    def __init__(self) -> None:
        self._queue: queue.Queue[TriggerEvent] = queue.Queue()
        self._handlers: list[EventHandler] = []
        self._running = False
        self._thread: threading.Thread | None = None

    def subscribe(self, handler: EventHandler) -> None:
        """Register a handler to process events."""
        self._handlers.append(handler)

    def emit(self, event: TriggerEvent) -> None:
        """Emit an event (thread-safe, non-blocking)."""
        logger.info("Event emitted: %s from %s", event.type.value, event.source)
        self._queue.put(event)

    def start(self) -> None:
        """Start the event processing loop in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._process_loop, daemon=True, name="event-bus")
        self._thread.start()
        logger.info("EventBus started")

    def stop(self) -> None:
        """Stop the event processing loop."""
        self._running = False
        if self._thread:
            self._queue.put(TriggerEvent(type=EventType.HOTKEY, source="__stop__"))
            self._thread.join(timeout=3)
        logger.info("EventBus stopped")

    def _process_loop(self) -> None:
        """Main loop: dequeue events and dispatch to handlers.

        J17: Run handlers in a thread pool so long-running agent.chat()
        doesn't block the event queue for other triggers.
        """
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="evt") as pool:
            while self._running:
                try:
                    event = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                if event.source == "__stop__":
                    break

                for handler in self._handlers:
                    pool.submit(self._safe_handler, handler, event)

    @staticmethod
    def _safe_handler(handler: EventHandler, event: TriggerEvent) -> None:
        """Run a handler with exception isolation."""
        try:
            handler(event)
        except Exception as e:
            logger.exception(
                "Handler failed for event %s: %s", event.type, e
            )


# Global singleton
event_bus = EventBus()
