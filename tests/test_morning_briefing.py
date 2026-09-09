"""Morning briefing — time-window gating, feature toggles, dedicated-conv routing.

Covers:
- within_window: HH:MM window logic (inside / outside / overnight / malformed)
- MorningBriefingTrigger: enabled switch + window gate before firing
- DaemonRunner: feature_briefing / feature_perf_monitor toggles actually apply
- ApiBridge.report_briefing: silent run in the pinned 「晨间播报」 conversation
  + clickable notification event (mirrors the 性能检测 pattern)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest

from agent_assistant.daemon.events import EventType, TriggerEvent
from agent_assistant.daemon.morning_briefing import (
    BriefingConfig,
    MorningBriefingTrigger,
    within_window,
)


def _at(hour: int, minute: int = 0) -> float:
    return datetime(2026, 8, 9, hour, minute).timestamp()


# ─── within_window ───────────────────────────────────────────────────────────


class TestWithinWindow:
    def test_inside_default_window(self):
        assert within_window(_at(8, 0), "06:30", "11:30") is True

    def test_before_window(self):
        assert within_window(_at(5, 59), "06:30", "11:30") is False

    def test_after_window(self):
        # The reported bug: briefing fired at 18:44
        assert within_window(_at(18, 44), "06:30", "11:30") is False

    def test_boundary_inclusive(self):
        assert within_window(_at(6, 30), "06:30", "11:30") is True
        assert within_window(_at(11, 30), "06:30", "11:30") is True

    def test_overnight_window(self):
        assert within_window(_at(23, 0), "22:00", "02:00") is True
        assert within_window(_at(1, 0), "22:00", "02:00") is True
        assert within_window(_at(12, 0), "22:00", "02:00") is False

    def test_malformed_bounds_fall_back_to_default(self):
        # A typo must not disable the gate (fire anytime) — falls back to 06:30–11:30
        assert within_window(_at(8, 0), "junk", "") is True
        assert within_window(_at(18, 0), "junk", "") is False
        assert within_window(_at(18, 0), "25:99", "xx") is False


# ─── Trigger gating (enabled switch + window) ────────────────────────────────


class _FakeBus:
    def __init__(self):
        self.events: list[TriggerEvent] = []

    def emit(self, event: TriggerEvent) -> None:
        self.events.append(event)


def _run_trigger(
    monkeypatch, config: BriefingConfig, *, in_window: bool = True
) -> list[TriggerEvent]:
    """Run _wait_and_fire synchronously with network/window stubbed; return emitted."""
    import agent_assistant.daemon.morning_briefing as mb

    bus = _FakeBus()
    monkeypatch.setattr(mb, "event_bus", bus)
    monkeypatch.setattr(mb, "is_network_ready", lambda *a, **k: True)
    monkeypatch.setattr(mb, "within_window", lambda **k: in_window)

    trigger = MorningBriefingTrigger(config)
    trigger._running = True  # simulate post-start state, run body inline
    trigger._wait_and_fire()
    return bus.events


class TestTriggerGating:
    def test_fires_inside_window(self, monkeypatch):
        events = _run_trigger(monkeypatch, BriefingConfig(delay_minutes=0))
        assert len(events) == 1
        assert events[0].type is EventType.MORNING_BRIEFING
        assert events[0].payload["goal"]
        goal = events[0].payload["goal"]
        assert "GitHub" in goal
        assert "新术语" in goal
        assert "news roundup" in goal.lower() or "NOT an AI news" in goal
        ctx = events[0].payload["context"]
        assert "GitHub" in ctx
        assert "headline" in ctx.lower() or "news" in ctx.lower()

    def test_skips_outside_window(self, monkeypatch):
        events = _run_trigger(
            monkeypatch, BriefingConfig(delay_minutes=0), in_window=False
        )
        assert events == []

    def test_disabled_never_fires(self, monkeypatch):
        events = _run_trigger(
            monkeypatch, BriefingConfig(delay_minutes=0, enabled=False)
        )
        assert events == []


# ─── DaemonRunner: toggles + settings wiring ─────────────────────────────────


class TestDaemonRunnerToggles:
    def _make_runner(self, monkeypatch, settings: dict[str, str]):
        from agent_assistant.daemon import runner as runner_mod

        monkeypatch.setattr(
            runner_mod, "_setting", lambda k, d="": settings.get(k, d)
        )
        return runner_mod.DaemonRunner()

    def test_briefing_toggle_off_disables_trigger(self, monkeypatch):
        runner = self._make_runner(monkeypatch, {"feature_briefing": "off"})
        assert runner._morning_briefing._config.enabled is False

    def test_briefing_toggle_default_on(self, monkeypatch):
        runner = self._make_runner(monkeypatch, {})
        assert runner._morning_briefing._config.enabled is True

    def test_window_settings_propagate(self, monkeypatch):
        runner = self._make_runner(
            monkeypatch,
            {"briefing_window_start": "07:00", "briefing_window_end": "09:00"},
        )
        cfg = runner._morning_briefing._config
        assert cfg.window_start == "07:00"
        assert cfg.window_end == "09:00"

    def test_perf_toggle_off_skips_monitor_start(self, monkeypatch):
        from agent_assistant.daemon import runner as runner_mod

        runner = self._make_runner(monkeypatch, {"feature_perf_monitor": "off"})
        # Isolate side effects: registry write + real event bus thread
        monkeypatch.setattr(runner_mod, "is_registered", lambda: True)
        monkeypatch.setattr(runner_mod.event_bus, "start", lambda: None)
        monkeypatch.setattr(runner_mod.event_bus, "stop", lambda: None)
        monkeypatch.setattr(runner_mod.event_bus, "subscribe", lambda h: None)

        runner.start()
        try:
            assert runner._perf_monitor._running is False
        finally:
            runner.stop()

    def test_perf_toggle_on_starts_monitor(self, monkeypatch):
        from agent_assistant.daemon import runner as runner_mod

        runner = self._make_runner(monkeypatch, {"feature_perf_monitor": "on"})
        monkeypatch.setattr(runner_mod, "is_registered", lambda: True)
        monkeypatch.setattr(runner_mod.event_bus, "start", lambda: None)
        monkeypatch.setattr(runner_mod.event_bus, "stop", lambda: None)
        monkeypatch.setattr(runner_mod.event_bus, "subscribe", lambda h: None)

        runner.start()
        try:
            assert runner._perf_monitor._running is True
        finally:
            runner.stop()


# ─── Briefing routing: dedicated 「晨间播报」 conversation ────────────────────


class TestBriefingRouting:
    def test_bridge_routes_to_report_briefing(self, monkeypatch):
        from agent_assistant.daemon import runner as runner_mod

        monkeypatch.setattr(runner_mod, "_setting", lambda k, d="": d)
        sent: list[str] = []

        class FakeBridge:
            def report_briefing(self, message: str) -> None:
                sent.append(message)

        runner = runner_mod.DaemonRunner(bridge=FakeBridge())
        runner._handle_event(
            TriggerEvent(
                type=EventType.MORNING_BRIEFING,
                payload={"goal": "research goal"},
                source="test",
            )
        )
        assert len(sent) == 1
        assert "research goal" in sent[0]
        assert "GitHub repos" in sent[0]
        assert "yesterday's AI news" in sent[0]

    def test_legacy_agent_fallback_without_bridge(self, monkeypatch):
        from agent_assistant.daemon import runner as runner_mod

        monkeypatch.setattr(runner_mod, "_setting", lambda k, d="": d)
        sent: list[str] = []

        class FakeAgent:
            def chat(self, message: str) -> str:
                sent.append(message)
                return "ok"

        runner = runner_mod.DaemonRunner(agent=FakeAgent())
        runner._handle_event(
            TriggerEvent(
                type=EventType.MORNING_BRIEFING,
                payload={"goal": "g"},
                source="test",
            )
        )
        assert len(sent) == 1


# ─── report_briefing (bridge layer) ──────────────────────────────────────────


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    """Fresh ApiBridge wired to a tmp UIStore; captures pushed events."""
    import agent_assistant.ui.store as store_mod
    from agent_assistant.ui.bridge import ApiBridge
    from agent_assistant.ui.store import UIStore

    store = UIStore(tmp_path / "ui.db")
    monkeypatch.setattr(store_mod, "ui_store", store)

    b = ApiBridge()
    b.events = []
    monkeypatch.setattr(b, "_push_event", lambda t, d: b.events.append((t, d)))
    return b


def _wait_briefing_thread():
    for t in threading.enumerate():
        if t.name == "briefing-report":
            t.join(timeout=5)


class TestReportBriefing:
    def test_routes_to_dedicated_pinned_conversation(self, bridge):
        import agent_assistant.ui.store as store_mod

        bridge.set_message_handler(lambda text: "简报已生成")
        bridge.report_briefing("[Morning Briefing Triggered]\ngoal: ...")
        _wait_briefing_thread()

        convs = [
            c for c in store_mod.ui_store.list_conversations() if c["title"] == "晨间播报"
        ]
        assert len(convs) == 1
        assert convs[0]["pinned"] is True
        msgs = store_mod.ui_store.list_messages(convs[0]["id"])
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles

        reports = [d for t, d in bridge.events if t == "briefing_report"]
        assert reports and reports[0]["conv_id"] == convs[0]["id"]
        assert "晨间播报" in reports[0]["message"]

    def test_silent_true_during_run_false_after(self, bridge):
        seen = []

        def handler(text):
            seen.append(bridge.report_silent)
            return "ok"

        bridge.set_message_handler(handler)
        bridge.report_briefing("briefing")
        _wait_briefing_thread()
        assert seen == [True]
        assert bridge.report_silent is False

    def test_reuses_existing_conversation(self, bridge):
        import agent_assistant.ui.store as store_mod

        bridge.set_message_handler(lambda text: "ok")
        bridge.report_briefing("first")
        _wait_briefing_thread()
        bridge.report_briefing("second")
        _wait_briefing_thread()

        convs = [
            c for c in store_mod.ui_store.list_conversations() if c["title"] == "晨间播报"
        ]
        assert len(convs) == 1
        msgs = store_mod.ui_store.list_messages(convs[0]["id"])
        assert sum(1 for m in msgs if m["role"] == "user") == 2

    def test_active_conversation_untouched(self, bridge):
        """The whole point: briefing must not hijack the user's active chat."""
        import agent_assistant.ui.store as store_mod

        active = store_mod.ui_store.create_conversation("我的聊天")
        bridge._active_conv_id = active["id"]
        before = len(store_mod.ui_store.list_messages(active["id"]))

        bridge.set_message_handler(lambda text: "ok")
        bridge.report_briefing("briefing")
        _wait_briefing_thread()

        assert bridge._active_conv_id == active["id"]
        assert len(store_mod.ui_store.list_messages(active["id"])) == before
