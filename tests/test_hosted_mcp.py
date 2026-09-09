"""Tests for hosted MCP mode (stdio JSON-RPC + computer_task delegation)."""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from agent_assistant.hosted.bridge_confirm import BridgeConfirmProvider
from agent_assistant.hosted.mcp_server import (
    COMPUTER_TASK_TOOL,
    HostedMcpServer,
    instruction_from_arguments,
    serve,
)
from agent_assistant.hosted.stdio_connection import StdioConnection


class RecordingConn:
    """Captures respond/notify calls; scripts server_request replies."""

    def __init__(self, elicit_reply=None):
        self.responses = []
        self.errors = []
        self.progress = []
        self.server_requests = []
        self._elicit_reply = elicit_reply
        self._pending = {}

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def respond_error(self, request_id, code, message):
        self.errors.append((request_id, code, message))

    def send_progress(self, message, *, progress_token=None):
        self.progress.append(message)

    def server_request(self, method, params, timeout):
        self.server_requests.append((method, params, timeout))
        return self._elicit_reply

    def is_pending(self, request_id):
        return request_id in self._pending

    def resolve_response(self, request_id, result, error):
        self._pending.pop(request_id, None)


class FakeLoop:
    """Stand-in AgentLoop: emits one tool_call event, returns canned text."""

    last_instance = None

    def __init__(self, conversation_id="local", on_event=None, **kwargs):
        self.conversation_id = conversation_id
        self._on_event = on_event or (lambda e: None)
        self.cancelled = False
        FakeLoop.last_instance = self

    def chat(self, instruction, *, turn_id=None):
        from agent_assistant.agent.loop import AgentEvent

        self._on_event(AgentEvent(type="tool_call", content={"name": "launch_app"}))
        self._on_event(AgentEvent(
            type="tool_result",
            content={"name": "launch_app", "result": {"ok": True}},
        ))
        return f"已完成：{instruction}"

    def request_cancel(self):
        self.cancelled = True


@pytest.fixture
def patch_loop(monkeypatch):
    monkeypatch.setattr("agent_assistant.agent.loop.AgentLoop", FakeLoop)
    FakeLoop.last_instance = None
    return FakeLoop


# ---- StdioConnection framing / correlation ----

def test_connection_writes_ndjson():
    lines = []
    conn = StdioConnection(lines.append)
    conn.respond(1, {"ok": True})
    conn.notify("notifications/progress", {"message": "hi"})
    assert json.loads(lines[0]) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert json.loads(lines[1])["method"] == "notifications/progress"


def test_server_request_correlates_response():
    lines = []
    conn = StdioConnection(lines.append)
    results = {}

    def caller():
        results["r"] = conn.server_request("elicitation/create", {"message": "ok?"}, timeout=2.0)

    t = threading.Thread(target=caller)
    t.start()
    # The request was written with a high server id; reply to it.
    sent = json.loads(lines[-1])
    assert sent["method"] == "elicitation/create"
    assert sent["id"] >= 1_000_000
    conn.resolve_response(sent["id"], {"action": "accept"}, None)
    t.join(timeout=2)
    assert results["r"] == {"action": "accept"}


def test_server_request_times_out():
    conn = StdioConnection(lambda _l: None)
    assert conn.server_request("elicitation/create", {}, timeout=0.05) is None


def test_fail_all_pending_unblocks():
    lines = []
    conn = StdioConnection(lines.append)
    out = {}

    def caller():
        out["r"] = conn.server_request("elicitation/create", {}, timeout=5.0)

    t = threading.Thread(target=caller)
    t.start()
    conn.fail_all_pending()
    t.join(timeout=2)
    assert out["r"] is None


# ---- BridgeConfirmProvider ----

def test_confirm_accept():
    conn = RecordingConn(elicit_reply={"action": "accept"})
    provider = BridgeConfirmProvider(conn, timeout=1.0)
    assert provider.confirm("kill_process", "结束进程 X") is True
    assert conn.server_requests[0][0] == "elicitation/create"


def test_confirm_decline():
    conn = RecordingConn(elicit_reply={"action": "decline"})
    assert BridgeConfirmProvider(conn).confirm("move_file", "移动文件") is False


def test_confirm_timeout_denies():
    conn = RecordingConn(elicit_reply=None)
    assert BridgeConfirmProvider(conn).confirm("kill_process", "x") is False


# ---- HostedMcpServer request handling ----

def test_initialize_and_tools_list():
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    init_result = conn.responses[0][1]
    assert init_result["protocolVersion"]
    assert init_result["capabilities"]["tools"] == {}

    tools = conn.responses[1][1]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "computer_task"
    # Must be marked non-destructive so the host doesn't double-confirm.
    assert tools[0]["annotations"]["destructiveHint"] is False
    assert "instruction" in tools[0]["inputSchema"]["properties"]


def test_ping():
    conn = RecordingConn()
    HostedMcpServer(conn).handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert conn.responses[-1] == (9, {})


def test_unknown_method_errors():
    conn = RecordingConn()
    HostedMcpServer(conn).handle_message({"jsonrpc": "2.0", "id": 5, "method": "bogus/x"})
    assert conn.errors[-1][0] == 5
    assert conn.errors[-1][1] == -32601


def test_computer_task_runs_and_reports(patch_loop):
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    server._run_task(7, "打开网易云音乐", progress_token=None)  # run inline (no thread)

    assert conn.responses[-1][0] == 7
    result = conn.responses[-1][1]
    assert result["isError"] is False
    assert "已完成：打开网易云音乐" in result["content"][0]["text"]
    # progress events surfaced from the loop's tool_call (templated zh line)
    assert any("打开应用" in p for p in conn.progress)


def test_computer_task_rejects_unknown_tool():
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "rm_rf", "arguments": {}},
    })
    assert conn.responses[-1][1]["isError"] is True


def test_computer_task_accepts_string_and_alias_arguments(patch_loop):
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    server.handle_message({
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "computer_task", "arguments": "打开网易云音乐"},
    })
    deadline = time.time() + 2
    while time.time() < deadline and not conn.responses:
        time.sleep(0.01)
    assert conn.responses, "string arguments should start computer_task"
    assert "已完成：打开网易云音乐" in conn.responses[-1][1]["content"][0]["text"]

    conn2 = RecordingConn()
    server2 = HostedMcpServer(conn2)
    server2.handle_message({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "computer_task", "arguments": {"task": "放水手"}},
    })
    deadline = time.time() + 2
    while time.time() < deadline and not conn2.responses:
        time.sleep(0.01)
    assert "已完成：放水手" in conn2.responses[-1][1]["content"][0]["text"]


def test_computer_task_requires_instruction():
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "computer_task", "arguments": {}},
    })
    assert conn.responses[-1][1]["isError"] is True
    assert "instruction" in conn.responses[-1][1]["content"][0]["text"]


def test_cancelled_notification_cancels_active_loop(patch_loop):
    conn = RecordingConn()
    server = HostedMcpServer(conn)
    loop = FakeLoop(conversation_id="hosted-x")
    server._active[11] = loop
    server.handle_message({
        "jsonrpc": "2.0", "method": "notifications/cancelled",
        "params": {"requestId": 11},
    })
    assert loop.cancelled is True


# ---- end-to-end wire framing through serve() (no tools/call → no threads) ----

def test_hosted_tool_profile_excludes_chroma_tools():
    # Register into a fresh registry (avoid clobbering the global singleton).
    import agent_assistant.tools.register as reg
    from agent_assistant.tools.registry import ToolRegistry

    original = reg.tool_registry
    fresh = ToolRegistry()
    reg.tool_registry = fresh
    try:
        reg.register_hosted_tools()
        names = {t.name for t in fresh.all_tools()}
    finally:
        reg.tool_registry = original

    # Chroma-backed tools must NOT be advertised in the lean hosted build.
    assert "search_knowledge" not in names
    assert "read_attached_source" not in names
    assert "recall_memory" not in names
    assert "save_memory" not in names
    # Desktop-app-only / LianYu-overlapping tools are stripped in hosted:
    # deep research belongs to the LianYu side; notes/sessions/toasts/selection
    # are desktop-companion features with no place under LianYu's chat UI.
    assert "dispatch_research" not in names
    assert "save_note" not in names
    assert "create_session" not in names
    assert "get_selection" not in names
    assert "notify" not in names
    # SQLite-only profile tool + core desktop tools remain.
    assert "update_profile" in names
    assert "launch_app" in names
    assert "run_command" in names
    assert "dispatch_subagents" in names
    assert "web_search" in names  # internal web lookups for mixed tasks stay


def test_computer_task_description_routes_lianyu():
    """The description IS LianYu's routing prompt: it must fit the backend's
    1024-char cap, enumerate capabilities, and explicitly divert pure
    search/research requests away from the desktop engine."""
    desc = COMPUTER_TASK_TOOL["description"]
    assert len(desc) <= 1024
    assert "不要调用" in desc
    assert "深度调研" in desc
    assert "本地文件" in desc
    assert "终端命令" in desc
    # Must name the JSON field: 0.1.3's "传入一句自然语言" made the outer
    # model skip {instruction: ...} and bounce in 1s with no start log.
    assert "instruction" in desc
    assert "角色口吻" not in desc
    assert "实时进度" not in desc
    assert "立刻再调用" in desc
    assert "禁止只回复" in desc


def test_instruction_from_arguments_coerces_aliases_and_strings():
    assert instruction_from_arguments({"instruction": "打开网易云"}) == "打开网易云"
    assert instruction_from_arguments({"task": "放《水手》"}) == "放《水手》"
    assert instruction_from_arguments({"query": "播放水手"}) == "播放水手"
    assert instruction_from_arguments("打开网易云来首水手") == "打开网易云来首水手"
    assert instruction_from_arguments('{"instruction":"打开网易云"}') == "打开网易云"
    assert instruction_from_arguments({}) == ""
    assert instruction_from_arguments(None) == ""


def test_main_does_not_reconfigure_stdin():
    """reconfigure(stdin) can drop a buffered initialize and cause host timeout."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "agent_assistant" / "hosted" / "mcp_server.py"
    text = src.read_text(encoding="utf-8")
    assert 'reconfigure(encoding="utf-8")' in text
    assert 'for stream_name in ("stdin", "stdout")' not in text
    assert "Do NOT reconfigure stdin" in text


def test_serve_wire_roundtrip(monkeypatch):
    # Avoid touching the real confirm gate global across tests.
    import agent_assistant.tools.confirm as confirm_mod
    monkeypatch.setattr(confirm_mod.confirm_gate, "provider", None, raising=False)

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout)

    out_lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    ids = {m.get("id"): m for m in out_lines}
    assert ids[1]["result"]["serverInfo"]["name"] == "agent-assistant-hosted"
    assert ids[2]["result"]["tools"][0]["name"] == "computer_task"
