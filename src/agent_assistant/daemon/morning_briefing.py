"""Morning briefing trigger — background daemon component.

Fires after login delay + network-ready, then emits a MORNING_BRIEFING event.
The main agent handler dispatches the deep-research sub-agent with a fixed goal.

Per docs/04-features.md §4.7:
- Trigger: 5 min (configurable) after login + network ready
- Reuses deep-research sub-agent with fixed goal prompt
- Content is new technical terms + trending AI GitHub repos — not industry news
- No fixed source list — agent searches the whole web
- Light guidance: GitHub Trending / Show HN / papers-with-code / technical blogs
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass

from agent_assistant.daemon.events import EventType, TriggerEvent, event_bus

logger = logging.getLogger(__name__)


@dataclass
class BriefingConfig:
    """Configuration for morning briefing trigger."""

    delay_minutes: float = 5.0  # Delay after daemon start (simulates login delay)
    network_check_interval: float = 10.0  # How often to check network
    network_check_host: str = "8.8.8.8"  # Host to ping for network check
    network_check_port: int = 53
    network_check_timeout: float = 3.0
    run_once: bool = True  # Only run once per daemon session
    enabled: bool = True  # Master switch (settings: feature_briefing)
    window_start: str = "06:30"  # Only fire within [window_start, window_end]
    window_end: str = "11:30"  # local time, HH:MM; start > end = overnight window


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """Parse 'HH:MM' → (hour, minute); None when malformed."""
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def within_window(now: float | None = None, start: str = "06:30", end: str = "11:30") -> bool:
    """True when local time falls inside [start, end] (HH:MM).

    Malformed bounds fall back to the default window so a typo can't make the
    briefing fire at random hours. start > end means an overnight window
    (e.g. 22:00–02:00).
    """
    s = _parse_hhmm(start) or _parse_hhmm("06:30")
    e = _parse_hhmm(end) or _parse_hhmm("11:30")
    t = time.localtime(now if now is not None else time.time())
    cur = t.tm_hour * 60 + t.tm_min
    lo = s[0] * 60 + s[1]
    hi = e[0] * 60 + e[1]
    if lo <= hi:
        return lo <= cur <= hi
    return cur >= lo or cur <= hi


def is_network_ready(host: str = "8.8.8.8", port: int = 53, timeout: float = 3.0) -> bool:
    """Check if network is available by attempting a TCP connection."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        return True
    except (socket.timeout, OSError):
        return False


MORNING_BRIEFING_GOAL = (
    "Produce a morning briefing with TWO sections only. Write the report in 简体中文. "
    "This is NOT an AI news roundup. Do NOT list yesterday's launches, funding, "
    "company announcements, model-release recaps, or 'what happened in AI'. "
    "\n\n"
    "Section 1 — 新术语 / 新方法论 (5–8 items):\n"
    "Find newly coined or recently viral technical names and methods in AI/agents/"
    "LLM-systems (examples of the *kind* of item: GraphEngineering, LoopEngineering, "
    "skill, context engineering — do not pad the list with these examples unless they "
    "are actually trending this week). For each: the term, a 2–4 sentence definition, "
    "why it appeared / what problem it names, and 1–2 source URLs.\n"
    "\n"
    "Section 2 — 近期热门 AI GitHub 项目 (5–8 repos):\n"
    "Recommend recently hot open-source AI projects (new repos or sudden star surges "
    "in the past 1–2 weeks). For each: owner/repo, one-line what it does, why it is "
    "hot now, approximate stars if available, and the github.com URL. Prefer tools, "
    "frameworks, evals, agent runtimes, and research code over news aggregators.\n"
    "\n"
    "Sources to prefer: GitHub Trending (python/typescript/jupyter), Show HN, "
    "paperswithcode, arXiv cs.AI/cs.CL discussion, Reddit r/LocalLLaMA, technical "
    "blogs. Skip general news sites and 'AI weekly recap' articles.\n"
    "Format as Markdown with those two H2 sections. No third 'news' section."
)

MORNING_BRIEFING_CONTEXT = (
    "Scheduled morning briefing. The reader wants new technical vocabulary and "
    "GitHub repos they can try — not industry headlines. Keep it scannable in "
    "3–4 minutes. Save the full report as a note after it is ready."
)


class MorningBriefingTrigger:
    """Fires the morning briefing after delay + network-ready."""

    def __init__(self, config: BriefingConfig | None = None) -> None:
        self._config = config or BriefingConfig()
        self._running = False
        self._thread: threading.Thread | None = None
        self._has_fired = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._wait_and_fire, daemon=True, name="morning-briefing"
        )
        self._thread.start()
        logger.info("MorningBriefing trigger armed (delay=%.1f min)", self._config.delay_minutes)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _wait_and_fire(self) -> None:
        if not self._config.enabled:
            logger.info("Morning briefing disabled (feature_briefing=off) — not firing")
            return

        # Phase 1: Wait for delay
        delay_seconds = self._config.delay_minutes * 60
        elapsed = 0.0
        while elapsed < delay_seconds and self._running:
            time.sleep(1.0)
            elapsed += 1.0

        if not self._running:
            return

        # Phase 1.5: Time-window gate — a "morning" briefing must not fire in
        # the afternoon/evening just because the app was launched then.
        if not within_window(
            start=self._config.window_start, end=self._config.window_end
        ):
            logger.info(
                "Morning briefing: outside window %s–%s — skipping this session",
                self._config.window_start,
                self._config.window_end,
            )
            return

        # Phase 2: Wait for network
        logger.info("Morning briefing: delay passed, waiting for network...")
        while self._running:
            if is_network_ready(
                self._config.network_check_host,
                self._config.network_check_port,
                self._config.network_check_timeout,
            ):
                break
            time.sleep(self._config.network_check_interval)

        if not self._running:
            return

        # Phase 3: Fire!
        if self._config.run_once and self._has_fired:
            return

        self._has_fired = True
        logger.info("Morning briefing: network ready, firing event!")

        event_bus.emit(TriggerEvent(
            type=EventType.MORNING_BRIEFING,
            payload={
                "goal": MORNING_BRIEFING_GOAL,
                "context": MORNING_BRIEFING_CONTEXT,
            },
            source="morning_briefing",
        ))
