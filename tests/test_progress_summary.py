"""Progress-summary loop: every N tool rounds → force a text-only summary
call, then resume with tools re-enabled."""

from __future__ import annotations

from typing import Any

import pytest

from agent_assistant.agent.loop import AgentLoop
from agent_assistant.config import settings


@pytest.fixture(autouse=True)
def _restore_loop_settings():
    prev_every = settings.progress_summary_every
    prev_max = settings.max_tool_rounds
    yield
    settings.progress_summary_every = prev_every
    settings.max_tool_rounds = prev_max


class _FakeToolCallDelta:
    def __init__(self, *, index: int, id: str, name: str, arguments: str = ""):
        self.index = index
        self.id = id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class _FakeChunk:
    def __init__(self, *, content: str = "", tool_calls: list[_FakeToolCallDelta] | None = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content: str | None = None

    @property
    def choices(self):
        return [self]

    @property
    def delta(self):
        return self


class _FakeStream:
    def __init__(self, chunks: list[_FakeChunk]):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_progress_summary_round_has_no_tools_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After `progress_summary_every` tool rounds, the next model call must
    have tools=[] and the model must answer with text (the summary), after
    which tools are re-enabled."""
    settings.progress_summary_every = 2
    settings.max_tool_rounds = 10

    call_log: list[dict[str, Any]] = []

    class FakeLLM:
        async def achat_stream(self, messages, tools=None, **kw):
            call_log.append({"tools": list(tools) if tools else [], "messages": list(messages)})
            # Round 1: tool call; Round 2: tool call; Round 3: summary text;
            # Round 4: final text (tools re-enabled, no nudge re-injected).
            if len(call_log) <= 2:
                return _FakeStream([
                    _FakeChunk(
                        tool_calls=[
                            _FakeToolCallDelta(
                                index=0,
                                id=f"call_{len(call_log)}",
                                name="noop_tool",
                            )
                        ]
                    )
                ])
            if len(call_log) == 3:
                return _FakeStream([_FakeChunk(content="SUMMARY")])
            return _FakeStream([_FakeChunk(content="DONE")])

    monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())

    # Register a no-op tool so the model has something to call.
    from agent_assistant.tools import tool_registry
    from agent_assistant.tools.base import Tool, ToolResult

    class NoopTool(Tool):
        name = "noop_tool"
        description = "test no-op"
        parameters = []

        def execute(self, **kwargs):
            return ToolResult.success({})

    tool_registry.register(NoopTool())

    loop = AgentLoop()
    events: list[str] = []
    loop._on_event = lambda e: events.append(e.type)

    # We can't call chat() because it appends user text and hydrates; drive
    # _run_rounds directly with a pre-seeded user message.
    loop._messages.append({"role": "user", "content": "test"})
    result = await loop._run_rounds()

    assert result == "DONE"
    # 4 API calls: tool, tool, summary(text-only), final(text with tools)
    assert len(call_log) == 4
    assert call_log[0]["tools"], "first round must have tools"
    assert call_log[1]["tools"], "second round must have tools"
    assert call_log[2]["tools"] == [], "summary round must have no tools"
    assert call_log[3]["tools"], "post-summary round must re-enable tools"
    # The summary round must contain the injected system nudge (not the main
    # system prompt at index 0).
    assert any(
        "进度汇报" in str(m.get("content", ""))
        for m in call_log[2]["messages"][1:]
        if m.get("role") == "system"
    )
    # The nudge did its job in round 3 — the post-summary round must NOT see
    # it anymore (it's removed the moment the report lands, so it doesn't
    # ride along in every later API call).
    assert not any(
        "进度汇报" in str(m.get("content", ""))
        for m in call_log[3]["messages"]
        if m.get("role") == "system"
    )
    # The forced progress round still happens (it breaks tool ping-pong), but
    # the report is no longer registered as a compaction checkpoint —
    # compaction now concatenates summary layers instead of merging a
    # pre-made one verbatim.
    assert not hasattr(loop._memory_manager, "pending_checkpoints")
    # Events must include the response.
    assert "response" in events


@pytest.mark.asyncio
async def test_ui_progress_summary_ends_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UI-dominated turn must stop after the forced summary, not resume."""
    settings.progress_summary_every = 2
    settings.max_tool_rounds = 10
    call_log: list[dict[str, Any]] = []

    class FakeLLM:
        async def achat_stream(self, messages, tools=None, **kw):
            call_log.append({"tools": list(tools) if tools else [], "messages": list(messages)})
            if len(call_log) <= 2:
                return _FakeStream([
                    _FakeChunk(
                        tool_calls=[
                            _FakeToolCallDelta(
                                index=0,
                                id=f"call_{len(call_log)}",
                                name="ui_inspect",
                            )
                        ]
                    )
                ])
            if len(call_log) == 3:
                return _FakeStream([_FakeChunk(content="卡在搜索结果")])
            return _FakeStream([_FakeChunk(content="SHOULD_NOT_RUN")])

    monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())

    from agent_assistant.tools import tool_registry
    from agent_assistant.tools.base import Tool, ToolResult

    class FakeInspect(Tool):
        name = "ui_inspect"
        description = "test inspect"
        parameters = []

        def execute(self, **kwargs):
            return ToolResult.success({"window": "大海 - 张雨生", "count": 0})

    prev_inspect = tool_registry.get("ui_inspect")
    tool_registry.register(FakeInspect())
    try:
        loop = AgentLoop()
        loop._messages.append({"role": "user", "content": "播放夜曲"})
        result = await loop._run_rounds()
    finally:
        if prev_inspect is not None:
            tool_registry.register(prev_inspect)
        else:
            tool_registry.unregister("ui_inspect")

    assert result == "卡在搜索结果"
    assert len(call_log) == 3
    assert call_log[2]["tools"] == []
    assert any(
        "最终回复" in str(m.get("content", ""))
        for m in call_log[2]["messages"]
        if m.get("role") == "system"
    )


def test_ui_stall_helpers():
    from agent_assistant.agent.loop import (
        _should_force_ui_stop,
        _ui_loop_stalled,
        _ui_turn_is_desktop,
    )

    assert _ui_turn_is_desktop(["ui_inspect", "ui_click"]) is True
    assert _ui_turn_is_desktop(["web_search", "read_page"]) is False
    assert _ui_loop_stalled(["ui_inspect"] * 7) is False
    assert _ui_loop_stalled(["ui_inspect"] * 8) is True
    assert _should_force_ui_stop(["ui_inspect"] * 9) is False
    assert _should_force_ui_stop(["ui_inspect"] * 10) is True
    assert _should_force_ui_stop(["web_search"] * 10) is False


@pytest.mark.asyncio
async def test_progress_summary_not_triggered_before_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only 1 tool round and threshold=2, no summary is forced."""
    settings.progress_summary_every = 2
    settings.max_tool_rounds = 10

    call_log: list[dict[str, Any]] = []

    class FakeLLM:
        async def achat_stream(self, messages, tools=None, **kw):
            call_log.append({"tools": list(tools) if tools else [], "messages": list(messages)})
            if len(call_log) == 1:
                return _FakeStream([
                    _FakeChunk(
                        tool_calls=[
                            _FakeToolCallDelta(index=0, id="call_1", name="noop_tool")
                        ]
                    )
                ])
            return _FakeStream([_FakeChunk(content="DONE")])

    monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())

    from agent_assistant.tools import tool_registry
    from agent_assistant.tools.base import Tool, ToolResult

    class NoopTool(Tool):
        name = "noop_tool"
        description = "test no-op"
        parameters = []

        def execute(self, **kwargs):
            return ToolResult.success({})

    tool_registry.register(NoopTool())

    loop = AgentLoop()
    loop._messages.append({"role": "user", "content": "test"})
    result = await loop._run_rounds()

    assert result == "DONE"
    assert len(call_log) == 2
    assert call_log[0]["tools"], "tool round must have tools"
    assert call_log[1]["tools"], "final round must still have tools"
    # No system nudge injected.
    assert not any(
        "进度汇报" in str(m.get("content", ""))
        for m in call_log[1]["messages"]
        if m.get("role") == "system"
    )
