"""Mid-turn compaction must not cause task amnesia.

Regression for the incident: 9 tool rounds into 「帮我打开网易云播放夜曲」,
memory compaction wiped the whole turn (roles=['system'] on round 10's API
call), so the model answered with an idle greeting. These tests pin:
1. The in-flight turn (last user message onward) survives compaction.
2. After compaction, the very next API call's system prompt carries the
   rolling summary (loop refreshes it immediately, not next user turn).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_assistant.agent.loop import AgentLoop
from agent_assistant.memory.manager import MemoryManager


class _FakeToolCallDelta:
    def __init__(self, *, index: int, id: str, name: str):
        self.index = index
        self.id = id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": "{}"})()


class _FakeChunk:
    def __init__(self, *, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None

    @property
    def choices(self):
        return [self]

    @property
    def delta(self):
        return self


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _mock_summarizer(text: str) -> str:
    return f"Summary of dropped context ({len(text)} chars): task was playing a song."


@pytest.mark.asyncio
async def test_midturn_compaction_keeps_task_and_injects_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_assistant.tools import tool_registry
    from agent_assistant.tools.base import Tool, ToolResult

    # Avoid touching real profile store / Chroma in tests.
    class _FakeMemoryService:
        def build_profile_additions(self) -> str:
            return ""

    monkeypatch.setattr(
        "agent_assistant.agent.loop.memory_service", _FakeMemoryService()
    )

    class BigTool(Tool):
        name = "big_tool"
        description = "returns a large payload to blow the token budget"
        parameters = []

        def execute(self, **kwargs):
            return ToolResult.success({"blob": "tree " * 400})

    tool_registry.register(BigTool())

    call_log: list[dict[str, Any]] = []

    class FakeLLM:
        async def achat_stream(self, messages, tools=None, **kw):
            call_log.append([dict(m) for m in messages])
            if len(call_log) <= 3:
                return _FakeStream([
                    _FakeChunk(tool_calls=[
                        _FakeToolCallDelta(
                            index=0, id=f"call_{len(call_log)}", name="big_tool"
                        )
                    ])
                ])
            return _FakeStream([_FakeChunk(content="DONE")])

    monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())

    loop = AgentLoop(
        memory_manager=MemoryManager(
            token_budget=300,  # tiny: compaction fires once the old turn is fat
            summary_cap=200,
            soft_rounds=1,
            summarizer=_mock_summarizer,
        )
    )
    # A completed OLD turn gives compaction something legitimate to drop
    # (the in-flight turn itself is protected and must survive whole).
    loop._messages.append({"role": "user", "content": "old question " + "word " * 300})
    loop._messages.append({"role": "assistant", "content": "old answer " + "word " * 300})
    loop._messages.append({"role": "user", "content": "帮我打开网易云播放夜曲"})
    result = await loop._run_rounds()

    assert result == "DONE"
    assert len(call_log) == 4

    # Compaction dropped the old completed turn.
    assert loop._memory_manager.dropped_count > 0

    # EVERY API call after the first must still contain the in-flight task:
    # the last user message (and everything after it) is never dropped.
    for i, msgs in enumerate(call_log):
        user_texts = [
            str(m.get("content", "")) for m in msgs if m.get("role") == "user"
        ]
        assert any(
            "帮我打开网易云播放夜曲" in t for t in user_texts
        ), f"call {i + 1} lost the in-flight user task"

    # After compaction, the system prompt of the NEXT call carries the
    # rolling summary header — the model never sees a bare system prompt.
    summary_seen = any(
        "Conversation Summary" in str(msgs[0].get("content", ""))
        for msgs in call_log[1:]
    )
    assert summary_seen, "rolling summary never re-injected after compaction"
