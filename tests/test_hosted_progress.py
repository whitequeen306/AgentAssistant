"""Hosted progress narration: loop events → user-facing MCP progress lines."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent_assistant.agent.loop import AgentEvent, AgentLoop
from agent_assistant.config import settings
from agent_assistant.hosted.mcp_server import (
    _ProgressNarrator,
    describe_tool_progress,
)


class _Conn:
    def __init__(self):
        self.progress = []

    def send_progress(self, message, *, progress_token=None):
        self.progress.append((message, progress_token))


# ---- describe_tool_progress ----

def test_progress_line_uses_zh_label_and_purpose():
    line = describe_tool_progress(
        "run_command", '{"command": "Get-ChildItem", "purpose": "查看桌面文件"}'
    )
    assert line == "正在运行命令「查看桌面文件」…"


def test_progress_line_prefers_text_for_typing():
    line = describe_tool_progress("ui_type", {"target": "e12", "text": "大海"})
    assert line == "正在输入文字「大海」…"


def test_progress_line_without_detail():
    assert describe_tool_progress("ui_inspect", {}) == "正在查看界面控件…"


def test_progress_line_truncates_long_detail():
    line = describe_tool_progress("web_search", {"query": "x" * 60})
    assert line.endswith("…」…")
    assert "x" * 25 not in line


def test_progress_line_tolerates_bad_json_and_unknown_tool():
    assert describe_tool_progress("bogus_tool", "not-json{") == "正在bogus_tool…"


# ---- _ProgressNarrator ----

def test_narrator_flushes_model_narration_before_tool_line():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token="req-1")
    narrator.handle(AgentEvent(type="response_chunk", content="先打开"))
    narrator.handle(AgentEvent(type="response_chunk", content="网易云音乐"))
    narrator.handle(AgentEvent(
        type="tool_call",
        content={"name": "launch_app", "arguments": '{"name": "网易云音乐"}'},
    ))
    # Narrated round: the templated status line is suppressed (no double-talk).
    assert conn.progress == [("先打开网易云音乐", "req-1")]


def test_narrator_templates_quiet_rounds_but_not_inspect():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token=None)
    # No narration buffered → templated line for action tools…
    narrator.handle(AgentEvent(
        type="tool_call",
        content={"name": "ui_type", "arguments": '{"target": "e12", "text": "大海"}'},
    ))
    # …but pure observation stays silent.
    narrator.handle(AgentEvent(
        type="tool_call",
        content={"name": "ui_inspect", "arguments": "{}"},
    ))
    assert conn.progress == [("正在输入文字「大海」…", None)]


def test_narrator_silent_on_success_but_notes_failure():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token=None)
    narrator.handle(AgentEvent(
        type="tool_result",
        content={"name": "ui_click", "result": {"ok": True}},
    ))
    assert conn.progress == []
    narrator.handle(AgentEvent(
        type="tool_result",
        content={"name": "ui_click", "result": {"ok": False}},
    ))
    assert conn.progress == [("这一步没成功，换个方式试试…", None)]


def test_narrator_forwards_response_first_line():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token="t")
    narrator.handle(AgentEvent(type="response", content="已经找到三首歌\n正在逐一试听"))
    assert conn.progress == [("已经找到三首歌", "t")]


def test_narrator_drops_stale_narration_on_final_response():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token=None)
    narrator.handle(AgentEvent(type="response_chunk", content="半截解说"))
    narrator.handle(AgentEvent(type="response", content="任务完成"))
    # buffered chunks must not leak ahead of the final response line
    assert conn.progress == [("任务完成", None)]


def test_narrator_thinking_drops_orphaned_chunks():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token=None)
    narrator.handle(AgentEvent(type="response_chunk", content="半截解说"))
    narrator.handle(AgentEvent(type="thinking"))
    narrator.handle(AgentEvent(
        type="tool_call",
        content={"name": "ui_type", "arguments": '{"text": "大海"}'},
    ))
    # Orphaned narration must not leak into the next round's template line.
    assert conn.progress == [("正在输入文字「大海」…", None)]


def test_narrator_never_raises_on_weird_events():
    conn = _Conn()
    narrator = _ProgressNarrator(conn, progress_token=None)
    narrator.handle(AgentEvent(type="tool_call", content=None))
    narrator.handle(AgentEvent(type="thinking"))
    narrator.handle(AgentEvent(type="response", content=None))
    assert conn.progress == []


# ---- hosted narration prompt section ----

@pytest.fixture()
def restore_narration():
    prev = settings.hosted_narration
    try:
        yield
    finally:
        settings.hosted_narration = prev


def test_hosted_prompt_includes_narration_section(restore_narration):
    settings.hosted_narration = True
    loop = AgentLoop(conversation_id="t-narration")
    with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
        mock_ms.build_profile_additions.return_value = ""
        mock_ms.build_study_profile_section.return_value = ""
        loop._refresh_system_prompt()
    assert "现场解说" in loop.messages[0]["content"]
    # Milestone-based narration: not every micro-step (would be noisy).
    assert "不要每步都解说" in loop.messages[0]["content"]


def test_desktop_prompt_excludes_narration_section(restore_narration):
    settings.hosted_narration = False
    loop = AgentLoop(conversation_id="t-plain")
    with patch("agent_assistant.agent.loop.memory_service") as mock_ms:
        mock_ms.build_profile_additions.return_value = ""
        mock_ms.build_study_profile_section.return_value = ""
        loop._refresh_system_prompt()
    assert "现场解说" not in loop.messages[0]["content"]
