"""Scene mode: test opens a fresh conversation; tools expose Chinese labels."""

from __future__ import annotations

import threading
import time

import pytest

from agent_assistant.tools.labels import tool_label
from agent_assistant.tools.register import register_all_tools
from agent_assistant.ui.bridge import SCENE_INJECTION_TEMPLATE, ApiBridge
from agent_assistant.ui.store import UIStore


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    import agent_assistant.ui.store as store_mod

    store = UIStore(tmp_path / "ui.db")
    monkeypatch.setattr(store_mod, "ui_store", store)

    b = ApiBridge()
    b.events = []
    monkeypatch.setattr(
        b, "_push_event", lambda t, d: b.events.append((t, d or {}))
    )
    b.set_message_handler(lambda text: "ok")
    yield b, store
    for t in threading.enumerate():
        if t.name in ("scene-test", "ui-message"):
            t.join(timeout=3)
    store.close()


def test_tool_label_launch_app():
    assert tool_label("launch_app") == "打开应用"


def test_get_tools_includes_chinese_label():
    register_all_tools()
    b = ApiBridge()
    tools = b.get_tools()
    by_name = {t["name"]: t for t in tools}
    assert "launch_app" in by_name
    assert by_name["launch_app"]["label"] == "打开应用"
    name_param = next(
        p for p in by_name["launch_app"]["parameters"] if p["name"] == "name"
    )
    assert name_param["label"] == "名称"


def test_get_tools_scene_whitelist_excludes_agent_internals():
    register_all_tools()
    b = ApiBridge()
    names = {t["name"] for t in b.get_tools()}
    assert "launch_app" in names
    assert "set_volume" in names
    assert "toggle_notifications" in names
    # Agent-internal tools must not appear in scene action picker
    assert "write_file" not in names
    assert "web_search" not in names
    assert "recall_memory" not in names
    assert "dispatch_research" not in names


def test_scene_injection_template_is_chinese_and_direct():
    text = SCENE_INJECTION_TEMPLATE.format(
        name="游戏模式",
        actions='[{"tool":"launch_app","args":{"name":"逆战"}}]',
    )
    assert "用户触发了场景" in text
    assert "recall_memory" in text
    assert "逆战" in text


def test_test_scene_uses_new_conversation(bridge):
    b, store = bridge
    old = store.create_conversation()
    b._active_conv_id = old["id"]
    store.add_message(old["id"], "user", "请不要打扰这条对话")

    scene = store.save_scene({
        "name": "游戏模式",
        "trigger_phrases": ["游戏模式"],
        "actions": [{"tool": "launch_app", "args": {"name": "逆战"}}],
    })

    b.test_scene(scene["id"])

    deadline = time.time() + 2.0
    while time.time() < deadline and not any(t == "scene_test" for t, _ in b.events):
        time.sleep(0.02)

    scene_test = next(d for t, d in b.events if t == "scene_test")
    assert scene_test["conv_id"] != old["id"]
    assert b._active_conv_id == scene_test["conv_id"]

    old_msgs = store.list_messages(old["id"])
    assert all("测试场景" not in (m.get("content") or "") for m in old_msgs)

    new_msgs = store.list_messages(scene_test["conv_id"])
    assert any("测试场景" in (m.get("content") or "") for m in new_msgs)
