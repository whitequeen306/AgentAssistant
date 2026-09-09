"""Per-conversation AgentPool isolation."""

from __future__ import annotations

import threading

from agent_assistant.agent.loop import AgentLoop
from agent_assistant.agent.pool import AgentPool
from agent_assistant.subagents.context import current_execution_context


def test_ensure_creates_distinct_loops():
    pool = AgentPool()
    a = pool.ensure("conv-a", [{"role": "user", "content": "hello A"}])
    b = pool.ensure("conv-b", [{"role": "user", "content": "hello B"}])
    assert a is not b
    assert a is pool.get("conv-a")
    assert "hello A" in a.messages[1]["content"]
    assert "hello B" in b.messages[1]["content"]


def test_ensure_does_not_overwrite_live_loop():
    pool = AgentPool()
    loop = pool.ensure("c1", [{"role": "user", "content": "first"}])
    loop._messages.append({
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "t1", "function": {"name": "web_search"}}],
    })
    # Switch-like ensure with stored text must keep the live tool_calls
    again = pool.ensure("c1", [{"role": "user", "content": "stale reload"}])
    assert again is loop
    assert any(m.get("tool_calls") for m in loop.messages)


def test_discard_removes_loop():
    pool = AgentPool()
    pool.ensure("x", [])
    assert "x" in pool
    pool.discard("x")
    assert "x" not in pool
    assert pool.active_count() == 0


def test_each_loop_has_private_memory_manager():
    a = AgentLoop()
    b = AgentLoop()
    assert a._memory_manager is not b._memory_manager


def test_chat_strips_trailing_duplicate_user(monkeypatch, tmp_path):
    """Store already has the user turn; chat() must not double-append it."""
    from agent_assistant.ui import store as store_mod

    db = tmp_path / "ui.db"
    ui = store_mod.UIStore(db_path=db)
    monkeypatch.setattr(store_mod, "ui_store", ui)

    conv = ui.create_conversation()
    cid = conv["id"]
    ui.add_message(cid, "user", "prior")
    ui.add_message(cid, "assistant", "ok")
    ui.add_message(cid, "user", "new question")  # just persisted by send_message

    pool = AgentPool()
    # Patch chat to only inspect history after hydrate, without calling LLM
    loop_holder: dict = {}

    def fake_chat(self, text):
        loop_holder["msgs"] = list(self._messages)
        loop_holder["text"] = text
        return "reply"

    monkeypatch.setattr(AgentLoop, "chat", fake_chat)
    pool.chat(cid, "new question")

    user_texts = [
        m["content"] for m in loop_holder["msgs"] if m.get("role") == "user"
    ]
    # Hydrated with prior only; fake_chat receives text but our stub doesn't append.
    # What matters: load_history did not keep the trailing "new question".
    assert user_texts == ["prior"]
    assert loop_holder["text"] == "new question"


def test_chat_passes_conversation_and_turn_context(monkeypatch):
    pool = AgentPool()
    seen = {}

    async def fake_run_rounds(self):
        seen["context"] = current_execution_context()
        return "reply"

    monkeypatch.setattr(AgentLoop, "_run_rounds", fake_run_rounds)

    assert pool.chat("conv-a", "hello", turn_id="msg-1") == "reply"
    context = seen["context"]
    assert context.conversation_id == "conv-a"
    assert context.parent_turn_id == "msg-1"
    assert context.owner_id.startswith("main:conv-a:msg-1:")
    assert pool.get("conv-a").conversation_id == "conv-a"


def test_request_cancel_unknown_conversation_does_not_cancel_all(monkeypatch):
    pool = AgentPool()
    first = pool.ensure("conv-a")
    second = pool.ensure("conv-b")
    cancelled = []
    monkeypatch.setattr(first, "request_cancel", lambda: cancelled.append("a"))
    monkeypatch.setattr(second, "request_cancel", lambda: cancelled.append("b"))

    assert pool.request_cancel("missing") is False
    assert cancelled == []


def test_concurrent_first_chat_publishes_only_hydrated_loop(monkeypatch):
    from agent_assistant.ui import store as store_mod

    load_started = threading.Event()
    allow_load = threading.Event()
    second_done = threading.Event()
    snapshots = []

    class BlockingStore:
        def list_messages(self, conv_id):
            load_started.set()
            assert allow_load.wait(timeout=2)
            return [{"role": "user", "content": "prior"}]

    def fake_chat(self, text):
        snapshots.append((self, list(self._messages)))
        return "reply"

    monkeypatch.setattr(store_mod, "ui_store", BlockingStore())
    monkeypatch.setattr(AgentLoop, "chat", fake_chat)
    pool = AgentPool()

    first = threading.Thread(target=pool.chat, args=("conv-a", "new"))

    def run_second():
        pool.chat("conv-a", "new")
        second_done.set()

    second = threading.Thread(target=run_second)
    first.start()
    assert load_started.wait(timeout=2)
    second.start()
    second_was_blocked = not second_done.wait(timeout=0.1)
    try:
        allow_load.set()
        first.join(timeout=2)
        second.join(timeout=2)
    finally:
        allow_load.set()

    assert second_was_blocked
    assert len(snapshots) == 2
    assert snapshots[0][0] is snapshots[1][0]
    assert all(
        any(message.get("content") == "prior" for message in messages)
        for _, messages in snapshots
    )
