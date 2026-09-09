"""Execution-context propagation and isolation."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from agent_assistant.agent.loop import AgentLoop
from agent_assistant.subagents.context import (
    AgentExecutionContext,
    MissingExecutionContext,
    bind_execution_context,
    current_execution_context,
    require_execution_context,
)


def _context(
    conversation_id: str = "conv-a",
    parent_turn_id: str = "msg-1",
) -> AgentExecutionContext:
    return AgentExecutionContext(
        conversation_id=conversation_id,
        parent_turn_id=parent_turn_id,
        owner_id=f"main:{conversation_id}:{parent_turn_id}",
        cancel_event=threading.Event(),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("conversation_id", ""),
        ("parent_turn_id", "  "),
        ("owner_id", ""),
        ("cancel_event", object()),
    ],
)
def test_execution_context_validates_required_fields(field, value):
    values = {
        "conversation_id": "conv-a",
        "parent_turn_id": "msg-1",
        "owner_id": "main:conv-a:msg-1",
        "cancel_event": threading.Event(),
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        AgentExecutionContext(**values)


def test_bind_context_resets_after_normal_and_exception():
    assert current_execution_context() is None
    context = _context()

    with bind_execution_context(context):
        assert require_execution_context() is context
    assert current_execution_context() is None

    with pytest.raises(RuntimeError), bind_execution_context(context):
        raise RuntimeError("boom")
    assert current_execution_context() is None

    with pytest.raises(MissingExecutionContext, match="execution context"):
        require_execution_context()


def test_contexts_are_isolated_across_async_tasks_and_threads():
    async def run_one(conversation_id: str):
        context = _context(conversation_id, f"turn-{conversation_id}")
        with bind_execution_context(context):
            await asyncio.sleep(0)
            threaded = await asyncio.to_thread(current_execution_context)
            return current_execution_context(), threaded

    async def run_all():
        return await asyncio.gather(run_one("a"), run_one("b"))

    results = asyncio.run(run_all())
    assert [pair[0].conversation_id for pair in results] == ["a", "b"]
    assert all(direct is threaded for direct, threaded in results)
    assert current_execution_context() is None


def test_agent_loop_binds_context_through_to_thread(monkeypatch):
    seen = {}
    loop = AgentLoop(conversation_id="conv-a")

    async def fake_run_rounds():
        seen["direct"] = current_execution_context()
        seen["thread"] = await asyncio.to_thread(current_execution_context)
        return "ok"

    monkeypatch.setattr(loop, "_run_rounds", fake_run_rounds)

    assert loop.chat("hello", turn_id="msg-1") == "ok"
    assert loop.conversation_id == "conv-a"
    assert seen["direct"] is seen["thread"]
    assert seen["direct"].parent_turn_id == "msg-1"
    assert seen["direct"].owner_id.startswith("main:conv-a:msg-1:")
    assert len(seen["direct"].owner_id.rsplit(":", 1)[1]) == 32
    assert seen["direct"].cancel_event is loop._cancel
    assert current_execution_context() is None


def test_reused_parent_turn_id_gets_unique_execution_owner(monkeypatch):
    seen = []
    loop = AgentLoop(conversation_id="conv-a")

    async def fake_run_rounds():
        seen.append(require_execution_context())
        return "ok"

    monkeypatch.setattr(loop, "_run_rounds", fake_run_rounds)

    loop.chat("first", turn_id="msg-1")
    loop.chat("second", turn_id="msg-1")

    assert [context.parent_turn_id for context in seen] == ["msg-1", "msg-1"]
    assert seen[0].owner_id != seen[1].owner_id
    assert all(
        context.owner_id.startswith("main:conv-a:msg-1:")
        and len(context.owner_id.rsplit(":", 1)[1]) == 32
        for context in seen
    )


def test_agent_loop_generates_turn_id_and_resets_after_error(monkeypatch):
    seen = {}
    loop = AgentLoop()

    async def failing_run_rounds():
        seen["context"] = require_execution_context()
        raise RuntimeError("llm failed")

    monkeypatch.setattr(loop, "_run_rounds", failing_run_rounds)

    with pytest.raises(RuntimeError, match="llm failed"):
        loop.chat("hello")

    assert seen["context"].parent_turn_id
    assert seen["context"].owner_id.startswith(
        f"main:local:{seen['context'].parent_turn_id}:"
    )
    assert current_execution_context() is None


def test_events_from_two_conversations_include_turn_identity(monkeypatch):
    from agent_assistant.agent import loop as loop_mod

    async def fake_achat_stream(**kwargs):
        async def chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="ok",
                            reasoning_content=None,
                            tool_calls=None,
                        )
                    )
                ]
            )

        return chunks()

    monkeypatch.setattr(loop_mod.llm_client, "achat_stream", fake_achat_stream)
    events = {}

    for conv_id, turn_id in (("conv-a", "msg-a"), ("conv-b", "msg-b")):
        captured = []
        loop = AgentLoop(conversation_id=conv_id, on_event=captured.append)
        monkeypatch.setattr(loop, "_refresh_system_prompt", lambda: None)
        assert loop.chat("hello", turn_id=turn_id) == "ok"
        events[conv_id] = captured

    for conv_id, turn_id in (("conv-a", "msg-a"), ("conv-b", "msg-b")):
        assert events[conv_id]
        assert all(event.conversation_id == conv_id for event in events[conv_id])
        assert all(event.parent_turn_id == turn_id for event in events[conv_id])
