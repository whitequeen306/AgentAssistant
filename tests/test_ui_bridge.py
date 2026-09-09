"""Focused tests for turn identity routing in the UI bridge."""

from __future__ import annotations

import json

from agent_assistant.ui import bridge as bridge_mod
from agent_assistant.ui import store as store_mod
from agent_assistant.ui.bridge import ApiBridge


def test_send_message_routes_persisted_message_id_as_turn_id(monkeypatch):
    calls = {}

    class FakeStore:
        def add_message(self, conv_id, role, content, msg_id=None):
            calls.setdefault("persisted", []).append(
                (conv_id, role, content, msg_id)
            )
            return msg_id or "store-generated-id"

        def set_title_if_empty(self, conv_id, text):
            pass

        def match_scene(self, text):
            return None

    class FakePool:
        def chat(self, conv_id, text, *, turn_id=None):
            calls["chat"] = (conv_id, text, turn_id)
            return ""

    class ImmediateThread:
        def __init__(self, *, target, args, **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(store_mod, "ui_store", FakeStore())
    monkeypatch.setattr(bridge_mod.threading, "Thread", ImmediateThread)

    bridge = ApiBridge()
    bridge._active_conv_id = "conv-a"
    bridge.set_agent_pool(FakePool())
    bridge.send_message("hello", msg_id=None)

    assert calls["persisted"][0] == ("conv-a", "user", "hello", None)
    assert calls["chat"] == ("conv-a", "hello", "store-generated-id")


def test_stream_event_payload_includes_turn_identity():
    scripts = []

    class FakeWindow:
        def evaluate_js(self, script):
            scripts.append(script)

    bridge = ApiBridge()
    bridge.set_window(FakeWindow())

    bridge.push_response(
        "done",
        conversation_id="conv-a",
        parent_turn_id="msg-1",
    )

    detail = scripts[0].split("detail: ", 1)[1].split("}));", 1)[0]
    payload = json.loads(detail)
    assert payload == {
        "type": "response",
        "text": "done",
        "conversation_id": "conv-a",
        "parent_turn_id": "msg-1",
    }
