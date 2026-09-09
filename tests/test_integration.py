"""Integration tests: streaming agent loop, event bus, bridge, voice fixes.

Tests multi-component collaboration without real LLM/GUI/audio.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── Streaming Agent Loop ─────────────────────────────────────────────────────


@dataclass
class FakeDelta:
    content: str | None = None
    tool_calls: list | None = None
    reasoning_content: str | None = None


@dataclass
class FakeChoice:
    delta: FakeDelta


@dataclass
class FakeChunk:
    choices: list


def _make_content_stream(text: str):
    """Create an async iterator that yields content chunks."""
    async def stream():
        for char in text:
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(content=char))])
    return stream()


def _make_reasoning_then_content_stream(reasoning: str, content: str):
    """Stream reasoning_content then final content (DeepSeek thinking mode)."""
    async def stream():
        for char in reasoning:
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
                reasoning_content=char
            ))])
        for char in content:
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(content=char))])
    return stream()


def _make_reasoning_then_tool_stream(
    reasoning: str, fn_name: str, fn_args: str, call_id: str = "call_1",
):
    """Stream CoT then a tool_call (must echo reasoning_content on assistant msg)."""
    @dataclass
    class FakeFn:
        name: str | None = None
        arguments: str | None = None

    @dataclass
    class FakeTCDelta:
        index: int
        id: str | None = None
        function: FakeFn | None = None

    async def stream():
        for char in reasoning:
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
                reasoning_content=char
            ))])
        yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
            tool_calls=[FakeTCDelta(index=0, id=call_id, function=FakeFn(name=fn_name))]
        ))])
        yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
            tool_calls=[FakeTCDelta(index=0, function=FakeFn(arguments=fn_args))]
        ))])
    return stream()


def _make_tool_stream(fn_name: str, fn_args: str, call_id: str = "call_1"):
    """Create an async iterator that yields tool_call deltas."""
    @dataclass
    class FakeFn:
        name: str | None = None
        arguments: str | None = None

    @dataclass
    class FakeTCDelta:
        index: int
        id: str | None = None
        function: FakeFn | None = None

    async def stream():
        # First chunk: tool call with id + name
        yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
            tool_calls=[FakeTCDelta(index=0, id=call_id, function=FakeFn(name=fn_name))]
        ))])
        # Second chunk: arguments
        yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
            tool_calls=[FakeTCDelta(index=0, function=FakeFn(arguments=fn_args))]
        ))])
    return stream()


def _make_multi_tool_stream(calls: list[tuple[str, str, str]]):
    """Create an async iterator that streams several tool_calls (one per index).

    `calls` is a list of (fn_name, fn_args, call_id) tuples — one per tool_call.
    """
    @dataclass
    class FakeFn:
        name: str | None = None
        arguments: str | None = None

    @dataclass
    class FakeTCDelta:
        index: int
        id: str | None = None
        function: FakeFn | None = None

    async def stream():
        for idx, (fn_name, fn_args, call_id) in enumerate(calls):
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
                tool_calls=[FakeTCDelta(
                    index=idx, id=call_id, function=FakeFn(name=fn_name)
                )]
            ))])
            yield FakeChunk(choices=[FakeChoice(delta=FakeDelta(
                tool_calls=[FakeTCDelta(
                    index=idx, function=FakeFn(arguments=fn_args)
                )]
            ))])
    return stream()


class TestStreamingLoop:
    """J12: Agent loop streams response_chunk events."""

    def test_content_stream_emits_chunks(self):
        """Streaming final response emits response_chunk per token."""
        from agent_assistant.agent.loop import AgentEvent, AgentLoop

        events: list[AgentEvent] = []
        loop = AgentLoop(on_event=events.append)

        fake_stream = _make_content_stream("Hello!")

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = AsyncMock(return_value=fake_stream)
            with patch("agent_assistant.agent.loop.memory_service"):
                result = asyncio.run(loop.achat("hi"))

        assert result == "Hello!"
        chunk_events = [e for e in events if e.type == "response_chunk"]
        assert len(chunk_events) == 6  # one per char
        assert "".join(e.content for e in chunk_events) == "Hello!"
        # Final response event still emitted
        assert any(e.type == "response" and e.content == "Hello!" for e in events)

    def test_tool_call_stream_executes_tool(self):
        """Streaming tool_call deltas are accumulated and executed."""
        from agent_assistant.agent.loop import AgentEvent, AgentLoop

        events: list[AgentEvent] = []
        loop = AgentLoop(on_event=events.append, max_rounds=2)

        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream("test_tool", '{"x": 1}')
            return _make_content_stream("Done!")

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"ok": True}
        mock_result.to_model_str.return_value = '{"ok": true}'

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = mock_result
                with patch("agent_assistant.agent.loop.memory_service"):
                    result = asyncio.run(loop.achat("do something"))

        assert result == "Done!"
        assert any(e.type == "tool_call" for e in events)
        assert any(e.type == "tool_result" for e in events)

    def test_tool_call_preserves_provider_id(self):
        """Happy path: when the provider streams a real tool_call id, it is
        preserved verbatim in both the assistant tool_calls and the tool
        message's tool_call_id (the fallback must never overwrite a real id)."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=2)
        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream("test_tool", '{"x": 1}', call_id="real_id_42")
            return _make_content_stream("Done!")

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"ok": True}
        mock_result.to_model_str.return_value = '{"ok": true}'

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = mock_result
                with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
                    mock_ms.get_memory_manager.return_value = None
                    asyncio.run(loop.achat("do something"))

        msgs = loop.messages
        asst = next(m for m in msgs if m.get("tool_calls"))
        tool = next(m for m in msgs if m.get("role") == "tool")
        assert asst["tool_calls"][0]["id"] == "real_id_42"
        assert tool["tool_call_id"] == "real_id_42"

    def test_tool_call_empty_id_gets_fallback(self):
        """When a provider streams tool_calls without an id, the loop fills a
        non-empty fallback so OpenAI can correlate tool_call_id. Without it the
        API rejects with 400 'assistant with tool_calls must be followed by
        tool messages' (no id to match)."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=2)
        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # call_id=None simulates a provider that omits tool_call ids
                return _make_tool_stream("test_tool", '{"x": 1}', call_id=None)
            return _make_content_stream("Done!")

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"ok": True}
        mock_result.to_model_str.return_value = '{"ok": true}'

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = mock_result
                with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
                    mock_ms.get_memory_manager.return_value = None
                    asyncio.run(loop.achat("do something"))

        msgs = loop.messages
        asst = next(m for m in msgs if m.get("tool_calls"))
        tool = next(m for m in msgs if m.get("role") == "tool")
        tc_id = asst["tool_calls"][0]["id"]
        assert tc_id, "tool_call id must be non-empty (provider omitted it)"
        assert tool["tool_call_id"] == tc_id, "tool message must match the id"

    def test_tool_result_serialize_failure_keeps_pairing(self):
        """A tool result that fails to serialize (e.g. non-JSON-serializable
        data like Path/datetime/set/bytes) must NOT orphan its
        assistant(tool_calls) message.

        Regression: without exception-safety, to_model_str() (json.dumps)
        raising skips that tool_call's tool message → the assistant(tool_calls)
        message has no matching tool message → every subsequent API call 400s
        with "insufficient tool messages following tool_calls message" (the
        project's wild tool-call errors)."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=2)
        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream("test_tool", '{"x": 1}', call_id="c1")
            return _make_content_stream("Done!")

        bad_result = MagicMock()
        bad_result.to_dict.return_value = {"ok": True, "data": "<non-serializable>"}
        bad_result.to_model_str.side_effect = TypeError("not JSON serializable")

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = bad_result
                with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
                    mock_ms.get_memory_manager.return_value = None
                    result = asyncio.run(loop.achat("do something"))

        assert result == "Done!"
        msgs = loop.messages
        asst = next(m for m in msgs if m.get("tool_calls"))
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(asst["tool_calls"]) == 1
        assert len(tool_msgs) == 1, "orphaned assistant(tool_calls): no tool message"
        assert tool_msgs[0]["tool_call_id"] == asst["tool_calls"][0]["id"]
        assert tool_msgs[0]["content"], "failure tool message must still carry content"

    def test_multi_tool_call_partial_failure_keeps_pairing(self):
        """When the model returns several tool_calls and one result fails to
        serialize, EVERY tool_call_id still gets a tool message.

        This is the exact 'insufficient tool messages following tool_calls
        message' 400 reproduction: 2 tool_calls but only 1 tool message."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=2)
        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_multi_tool_stream([
                    ("good_tool", '{"a": 1}', "good_id"),
                    ("bad_tool", '{"b": 2}', "bad_id"),
                ])
            return _make_content_stream("Done!")

        good_result = MagicMock()
        good_result.to_dict.return_value = {"ok": True}
        good_result.to_model_str.return_value = '{"ok": true}'

        bad_result = MagicMock()
        bad_result.to_dict.return_value = {"ok": True, "data": "<bad>"}
        bad_result.to_model_str.side_effect = TypeError("not JSON serializable")

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.side_effect = [good_result, bad_result]
                with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
                    mock_ms.get_memory_manager.return_value = None
                    result = asyncio.run(loop.achat("do something"))

        assert result == "Done!"
        msgs = loop.messages
        asst = next(m for m in msgs if m.get("tool_calls"))
        tc_ids = [tc["id"] for tc in asst["tool_calls"]]
        assert tc_ids == ["good_id", "bad_id"]
        tool_msgs = {m["tool_call_id"]: m for m in msgs if m.get("role") == "tool"}
        assert set(tool_msgs) == set(tc_ids), (
            "every tool_call_id must have a tool message or the API 400s"
        )

    def test_on_event_raise_does_not_orphan(self):
        """A UI event handler (on_event) that raises must not skip the tool
        message. Otherwise the assistant(tool_calls) message is orphaned and
        every later API call 400s — the same class as the serialization bug.
        """
        from agent_assistant.agent.loop import AgentLoop

        def boom(event):
            if event.type == "tool_call":
                raise RuntimeError("handler exploded")

        loop = AgentLoop(on_event=boom, max_rounds=2)
        call_count = [0]

        async def fake_achat_stream(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream("test_tool", '{"x": 1}', call_id="c1")
            return _make_content_stream("Done!")

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"ok": True}
        mock_result.to_model_str.return_value = '{"ok": true}'

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_achat_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = mock_result
                with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
                    mock_ms.get_memory_manager.return_value = None
                    result = asyncio.run(loop.achat("do something"))

        assert result == "Done!"
        msgs = loop.messages
        asst = next(m for m in msgs if m.get("tool_calls"))
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, "on_event raise must not skip the tool message"
        assert tool_msgs[0]["tool_call_id"] == asst["tool_calls"][0]["id"]

    def test_find_orphaned_tool_calls_detects_missing(self):
        """The pre-API diagnostic must flag an assistant(tool_calls) whose
        tool_call_id has no matching tool message (the 400 cause), and clear
        once the missing tool message is present."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=1)
        loop._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "a", "function": {"name": "t1"}},
                {"id": "b", "function": {"name": "t2"}},
            ]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},  # only 'a'
        ]
        orphans = loop._find_orphaned_tool_calls()
        assert len(orphans) == 1
        assert orphans[0]["tool_call_id"] == "b"
        assert orphans[0]["tool_name"] == "t2"

        # Add the missing tool message → no orphans
        loop._messages.append(
            {"role": "tool", "tool_call_id": "b", "content": "{}"}
        )
        assert loop._find_orphaned_tool_calls() == []

    def test_find_orphaned_tool_calls_requires_contiguous(self):
        """A tool message after an interrupting user turn still 400s — count
        it as orphaned so repair can re-insert immediately after tool_calls."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=1)
        loop._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_perf", "function": {"name": "perf_snapshot"}},
            ]},
            {"role": "user", "content": "deep research please"},  # race insert
            {"role": "tool", "tool_call_id": "call_perf", "content": "{}"},
        ]
        orphans = loop._find_orphaned_tool_calls()
        assert len(orphans) == 1
        assert orphans[0]["tool_call_id"] == "call_perf"

    def test_repair_orphaned_tool_calls_inserts_synthetic(self):
        """Repair must insert a synthetic tool failure immediately after the
        assistant(tool_calls) so the next API call is well-formed."""
        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(max_rounds=1)
        loop._messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_perf", "function": {"name": "perf_snapshot"}},
            ]},
            {"role": "user", "content": "deep research please"},
        ]
        n = loop._repair_orphaned_tool_calls()
        assert n == 1
        assert loop._find_orphaned_tool_calls() == []
        # Synthetic tool sits right after assistant, before the interrupting user
        roles = [m["role"] for m in loop._messages]
        assert roles[2:5] == ["assistant", "tool", "user"]
        assert loop._messages[3]["tool_call_id"] == "call_perf"
        import json as _json
        assert _json.loads(loop._messages[3]["content"])["ok"] is False

    def test_thinking_event_emitted(self):
        """Each round emits a thinking event."""
        from agent_assistant.agent.loop import AgentEvent, AgentLoop

        events: list[AgentEvent] = []
        loop = AgentLoop(on_event=events.append)

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = AsyncMock(
                return_value=_make_content_stream("ok")
            )
            with patch("agent_assistant.agent.loop.memory_service"):
                asyncio.run(loop.achat("hi"))

        assert any(e.type == "thinking" for e in events)

    def test_reasoning_content_emits_thinking_chunks(self):
        """DeepSeek CoT deltas become thinking_chunk events."""
        from agent_assistant.agent.loop import AgentEvent, AgentLoop

        events: list[AgentEvent] = []
        loop = AgentLoop(on_event=events.append)

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = AsyncMock(
                return_value=_make_reasoning_then_content_stream("why", "ok")
            )
            with patch("agent_assistant.agent.loop.memory_service"):
                asyncio.run(loop.achat("hi"))

        chunks = [e.content for e in events if e.type == "thinking_chunk"]
        assert "".join(chunks) == "why"
        assistant = [m for m in loop._messages if m["role"] == "assistant"][-1]
        assert assistant.get("reasoning_content") == "why"
        assert assistant["content"] == "ok"

    def test_tool_turn_keeps_reasoning_content(self):
        """When tools are used, assistant message must include reasoning_content."""
        from agent_assistant.agent.loop import AgentEvent, AgentLoop

        events: list[AgentEvent] = []
        loop = AgentLoop(on_event=events.append, max_rounds=2)

        call_n = [0]

        async def fake_stream(**_kwargs):
            call_n[0] += 1
            if call_n[0] == 1:
                return _make_reasoning_then_tool_stream(
                    "plan", "test_tool", '{"x": 1}', call_id="c_r"
                )
            return _make_content_stream("Done!")

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"ok": True}
        mock_result.to_model_str.return_value = '{"ok": true}'

        with patch("agent_assistant.agent.loop.llm_client") as mock_llm:
            mock_llm.achat_stream = fake_stream
            with patch("agent_assistant.agent.loop.tool_registry") as mock_reg:
                mock_reg.openai_schemas.return_value = []
                mock_reg.execute.return_value = mock_result
                with patch("agent_assistant.agent.loop.memory_service"):
                    asyncio.run(loop.achat("hi"))

        # First assistant (tool_calls) must echo CoT for DeepSeek API.
        assistant_tc = next(
            m for m in loop._messages
            if m["role"] == "assistant" and m.get("tool_calls")
        )
        assert assistant_tc.get("reasoning_content") == "plan"
        assert any(e.type == "thinking_chunk" for e in events)


# ─── Event Bus Async Dispatch (J17) ──────────────────────────────────────────


class TestEventBusAsync:
    """J17: Handlers run in thread pool, don't block queue."""

    def test_slow_handler_doesnt_block_fast(self):
        """A slow handler doesn't prevent the next event from dispatching."""
        from agent_assistant.daemon.events import EventType, EventBus, TriggerEvent

        bus = EventBus()
        results: list[str] = []
        lock = threading.Lock()

        def slow_handler(event: TriggerEvent):
            if event.payload.get("id") == "slow":
                time.sleep(0.3)
            with lock:
                results.append(event.payload.get("id", "?"))

        bus.subscribe(slow_handler)
        bus.start()

        # Emit slow then fast
        bus.emit(TriggerEvent(type=EventType.HOTKEY, payload={"id": "slow"}))
        bus.emit(TriggerEvent(type=EventType.HOTKEY, payload={"id": "fast"}))

        # Wait enough for both to complete
        time.sleep(0.8)
        bus.stop()

        # Both should have been processed (fast not blocked by slow)
        assert "slow" in results
        assert "fast" in results

    def test_handler_exception_isolated(self):
        """One handler crashing doesn't affect others."""
        from agent_assistant.daemon.events import EventType, EventBus, TriggerEvent

        bus = EventBus()
        results: list[str] = []

        def bad_handler(event: TriggerEvent):
            raise ValueError("boom")

        def good_handler(event: TriggerEvent):
            results.append("ok")

        bus.subscribe(bad_handler)
        bus.subscribe(good_handler)
        bus.start()

        bus.emit(TriggerEvent(type=EventType.HOTKEY, payload={}))
        time.sleep(0.3)
        bus.stop()

        assert "ok" in results


# ─── Bridge Event Push (J22 + J12) ───────────────────────────────────────────


class TestBridgeEvents:
    """J22/J12: Bridge pushes all event types to frontend."""

    def test_push_response_chunk(self):
        from agent_assistant.ui.bridge import ApiBridge

        bridge = ApiBridge()
        mock_window = MagicMock()
        bridge.set_window(mock_window)

        bridge.push_response_chunk("Hello")

        mock_window.evaluate_js.assert_called_once()
        js_call = mock_window.evaluate_js.call_args[0][0]
        assert "response_chunk" in js_call
        assert "Hello" in js_call

    def test_push_thinking(self):
        from agent_assistant.ui.bridge import ApiBridge

        bridge = ApiBridge()
        mock_window = MagicMock()
        bridge.set_window(mock_window)

        bridge.push_thinking()

        js_call = mock_window.evaluate_js.call_args[0][0]
        assert "thinking" in js_call

    def test_push_thinking_chunk(self):
        from agent_assistant.ui.bridge import ApiBridge

        bridge = ApiBridge()
        mock_window = MagicMock()
        bridge.set_window(mock_window)

        bridge.push_thinking_chunk("step one")

        js_call = mock_window.evaluate_js.call_args[0][0]
        assert "thinking_chunk" in js_call
        assert "step one" in js_call

    def test_push_voice_state(self):
        from agent_assistant.ui.bridge import ApiBridge

        bridge = ApiBridge()
        mock_window = MagicMock()
        bridge.set_window(mock_window)

        bridge.push_voice_state("listening")

        js_call = mock_window.evaluate_js.call_args[0][0]
        assert "listening" in js_call


# ─── TTS Sentence Split (J19) ────────────────────────────────────────────────


class TestTTSSentenceSplit:
    """J19: TTS splits text into sentences for streaming playback."""

    def test_split_chinese_sentences(self):
        from agent_assistant.voice.tts import TTSEngine

        sentences = TTSEngine._split_sentences("你好世界。今天天气不错！你觉得呢？")
        assert len(sentences) >= 2
        # Each sentence should be non-empty
        assert all(s.strip() for s in sentences)

    def test_split_english_sentences(self):
        from agent_assistant.voice.tts import TTSEngine

        sentences = TTSEngine._split_sentences(
            "Hello world. How are you? I am fine!"
        )
        assert len(sentences) >= 2

    def test_short_text_no_split(self):
        from agent_assistant.voice.tts import TTSEngine

        sentences = TTSEngine._split_sentences("Hi")
        assert sentences == ["Hi"]

    def test_merge_short_fragments(self):
        from agent_assistant.voice.tts import TTSEngine

        # Very short fragments should be merged
        sentences = TTSEngine._split_sentences("a. b. c. This is a longer sentence.")
        # Should not have 4 separate tiny sentences
        assert len(sentences) <= 3


# ─── Hotkey Combo Tracking (G1) ──────────────────────────────────────────────


class TestHotkeyCombo:
    """G1: Only stop recording when combo key releases while combo active."""

    def test_space_without_combo_doesnt_stop(self):
        from agent_assistant.voice.hotkey import HotkeyListener

        listener = HotkeyListener()
        listener._recording = True
        listener._combo_active = False  # combo NOT engaged

        # Simulate space release — should NOT stop
        event = MagicMock()
        event.name = "space"
        listener._on_release_check(event)

        assert listener._recording is True  # still recording

    def test_combo_key_release_stops(self):
        from agent_assistant.voice.hotkey import HotkeyListener

        listener = HotkeyListener()
        listener._recording = True
        listener._combo_active = True  # combo IS engaged

        event = MagicMock()
        event.name = "space"

        with patch.object(listener, "_stop_and_process") as mock_stop:
            listener._on_release_check(event)
            mock_stop.assert_called_once()

    def test_non_combo_key_ignored(self):
        from agent_assistant.voice.hotkey import HotkeyListener

        listener = HotkeyListener()
        listener._recording = True
        listener._combo_active = True

        event = MagicMock()
        event.name = "a"  # not a combo key
        listener._on_release_check(event)

        # Should still be recording (key 'a' is not in combo)
        assert listener._recording is True


# ─── Perf Monitor Thresholds (J18) ───────────────────────────────────────────


class TestPerfThresholds:
    """J18: Thresholds lowered to 80%/5s."""

    def test_default_thresholds(self):
        from agent_assistant.daemon.perf_monitor import PerfThresholds

        t = PerfThresholds()
        assert t.cpu_percent == 80.0
        assert t.memory_percent == 80.0
        assert t.sustained_seconds == 5.0
        assert t.check_interval == 2.0


# ─── STT Config (J20) ────────────────────────────────────────────────────────


class TestSTTConfig:
    """J20: STT model configurable via settings (sherpa-onnx + SenseVoice)."""

    def test_default_settings(self):
        from agent_assistant.config import settings

        # Empty by default → STTEngine resolves to data_dir/models/sense-voice/
        assert settings.sense_voice_model == ""
        assert settings.sense_voice_tokens == ""

    def test_engine_uses_resolved_default_path(self):
        from agent_assistant.voice.stt import STTEngine

        engine = STTEngine()
        assert engine._model.endswith("model.int8.onnx")
        assert engine._tokens.endswith("tokens.txt")
        assert engine._language == "auto"
        assert engine._provider == "cpu"

    def test_engine_override(self):
        from agent_assistant.voice.stt import STTEngine

        engine = STTEngine(
            model="/x/m.onnx", tokens="/x/t.txt", language="zh", provider="cuda"
        )
        assert engine._model == "/x/m.onnx"
        assert engine._tokens == "/x/t.txt"
        assert engine._language == "zh"
        assert engine._provider == "cuda"
