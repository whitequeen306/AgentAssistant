"""Tests for J23: single-instance IPC + invoke_on_file handler (08 §4.6).

Covers:
- IPC roundtrip: server receives message, client gets ack
- Client fails gracefully when no instance is running (missing/stale port file)
- bridge.invoke_on_file: new conversation + status push + agent injection
- Pending-file consumption at UI startup
"""

import threading
import time

import pytest

from agent_assistant.ipc import IPCServer, send_to_running_instance

# ─── IPC server / client ─────────────────────────────────────────────────────


class TestIPC:
    def test_roundtrip(self, tmp_path):
        port_file = tmp_path / "ipc.port"
        received = []
        got = threading.Event()

        def handler(msg):
            received.append(msg)
            got.set()

        server = IPCServer(handler, port_file=port_file)
        server.start()
        try:
            assert port_file.exists()
            ok = send_to_running_instance(
                {"type": "invoke_on_file", "path": "C:/x.txt"},
                port_file=port_file,
            )
            assert ok is True
            assert got.wait(timeout=3)
            assert received[0] == {"type": "invoke_on_file", "path": "C:/x.txt"}
        finally:
            server.stop()

    def test_no_port_file_returns_false(self, tmp_path):
        assert (
            send_to_running_instance(
                {"type": "invoke_on_file", "path": "x"},
                port_file=tmp_path / "missing.port",
            )
            is False
        )

    def test_stale_port_file_returns_false(self, tmp_path):
        # Port file exists but nothing is listening (crashed instance)
        port_file = tmp_path / "ipc.port"
        port_file.write_text("59999")
        assert (
            send_to_running_instance(
                {"type": "invoke_on_file", "path": "x"},
                port_file=port_file,
            )
            is False
        )

    def test_garbage_port_file_returns_false(self, tmp_path):
        port_file = tmp_path / "ipc.port"
        port_file.write_text("not-a-port")
        assert (
            send_to_running_instance({"type": "x"}, port_file=port_file) is False
        )

    def test_stop_removes_port_file(self, tmp_path):
        port_file = tmp_path / "ipc.port"
        server = IPCServer(lambda msg: None, port_file=port_file)
        server.start()
        assert port_file.exists()
        server.stop()
        assert not port_file.exists()

    def test_second_message_after_first(self, tmp_path):
        # Server keeps accepting (one connection per message)
        port_file = tmp_path / "ipc.port"
        received = []
        server = IPCServer(received.append, port_file=port_file)
        server.start()
        try:
            assert send_to_running_instance({"n": 1}, port_file=port_file)
            assert send_to_running_instance({"n": 2}, port_file=port_file)
            deadline = time.time() + 3
            while len(received) < 2 and time.time() < deadline:
                time.sleep(0.02)
            assert [m["n"] for m in received] == [1, 2]
        finally:
            server.stop()

    def test_invoke_on_text_roundtrip(self, tmp_path):
        # Browser-extension path: native host forwards {type: invoke_on_text, text}
        port_file = tmp_path / "ipc.port"
        received = []
        got = threading.Event()

        def handler(msg):
            received.append(msg)
            got.set()

        server = IPCServer(handler, port_file=port_file)
        server.start()
        try:
            ok = send_to_running_instance(
                {"type": "invoke_on_text", "text": "hello from browser"},
                port_file=port_file,
            )
            assert ok is True
            assert got.wait(timeout=3)
            assert received[0] == {"type": "invoke_on_text", "text": "hello from browser"}
        finally:
            server.stop()


# ─── invoke_on_file (bridge layer) ───────────────────────────────────────────


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
    monkeypatch.setattr(
        b, "_push_event", lambda t, d: b.events.append((t, d))
    )
    yield b
    # Drain background workers before closing the store (avoid closing
    # the SQLite handle while _process_message is still persisting)
    for t in threading.enumerate():
        if t.name in ("file-invoke", "text-invoke", "ui-message", "perf-report"):
            t.join(timeout=3)
    store.close()


def _wait_for_agent_input(bridge, path):
    """Set a capturing message handler; return (event, captured-list)."""
    done = threading.Event()
    captured = []

    def handler(text):
        captured.append(text)
        done.set()
        return "我已读取完成,可以进行我们的工作"

    bridge.set_message_handler(handler)
    bridge.invoke_on_file(str(path))
    return done, captured


class TestInvokeOnFile:
    def test_injects_spec_prompt(self, bridge, tmp_path):
        f = tmp_path / "report.md"
        f.write_text("hello", encoding="utf-8")
        done, captured = _wait_for_agent_input(bridge, f)
        assert done.wait(timeout=3)
        text = captured[0]
        # 08 §4.6 injection: path + tool hints + interactive-reading contract
        assert str(f) in text
        assert "read_file" in text
        assert "list_files" in text
        # narration contract: opening line BEFORE the first tool call
        assert "第一次调用读取工具之前" in text
        # wrap-up with follow-up options
        assert "后续选项" in text

    def test_creates_new_conversation_titled_by_name(self, bridge, tmp_path):
        import agent_assistant.ui.store as store_mod

        f = tmp_path / "notes.txt"
        f.write_text("x", encoding="utf-8")
        done, _ = _wait_for_agent_input(bridge, f)
        assert done.wait(timeout=3)
        convs = store_mod.ui_store.list_conversations()
        assert len(convs) == 1
        assert convs[0]["title"] == "notes.txt"
        assert bridge._active_conv_id == convs[0]["id"]

    def test_status_event_for_file(self, bridge, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        done, _ = _wait_for_agent_input(bridge, f)
        assert done.wait(timeout=3)
        statuses = [d for t, d in bridge.events if t == "status"]
        assert statuses and statuses[0]["text"] == "正在读取文件中..."

    def test_status_event_for_folder(self, bridge, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        done, _ = _wait_for_agent_input(bridge, folder)
        assert done.wait(timeout=3)
        statuses = [d for t, d in bridge.events if t == "status"]
        assert statuses and statuses[0]["text"] == "正在读取文件夹中..."

    def test_file_invoke_event_payload(self, bridge, tmp_path):
        folder = tmp_path / "proj"
        folder.mkdir()
        done, _ = _wait_for_agent_input(bridge, folder)
        assert done.wait(timeout=3)
        invokes = [d for t, d in bridge.events if t == "file_invoke"]
        assert len(invokes) == 1
        assert invokes[0]["path"] == str(folder)
        assert invokes[0]["is_dir"] is True
        assert invokes[0]["name"] == "proj"
        assert invokes[0]["conv_id"] == bridge._active_conv_id

    def test_assistant_reply_persisted(self, bridge, tmp_path):
        import agent_assistant.ui.store as store_mod

        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        done, _ = _wait_for_agent_input(bridge, f)
        assert done.wait(timeout=3)
        # _process_message persists the reply (S3: no double push)
        deadline = time.time() + 3
        msgs = []
        while time.time() < deadline:
            msgs = store_mod.ui_store.list_messages(bridge._active_conv_id)
            if msgs:
                break
            time.sleep(0.02)
        assert msgs and msgs[-1]["role"] == "assistant"
        assert "我已读取完成" in msgs[-1]["content"]

    def test_pending_file_consumed_once(self, bridge, tmp_path, monkeypatch):
        import agent_assistant.ui.bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "PENDING_FILE_DELAY", 0.0)
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")

        done = threading.Event()
        captured = []

        def handler(text):
            captured.append(text)
            done.set()
            return "ok"

        bridge.set_message_handler(handler)
        bridge.set_pending_file(str(f))
        bridge._consume_pending_file()
        assert done.wait(timeout=3)
        assert str(f) in captured[0]
        # Second consume is a no-op
        bridge._consume_pending_file()
        time.sleep(0.2)
        assert len(captured) == 1


# ─── invoke_on_text (bridge layer — browser extension path) ──────────────────


def _wait_for_agent_input_text(bridge, text):
    """Set a capturing message handler; invoke_on_text; return (event, captured)."""
    done = threading.Event()
    captured = []

    def handler(t):
        captured.append(t)
        done.set()
        return "已收到"

    bridge.set_message_handler(handler)
    bridge.invoke_on_text(text)
    return done, captured


class TestInvokeOnText:
    def test_injects_spec_prompt(self, bridge):
        done, captured = _wait_for_agent_input_text(bridge, "hello world text")
        assert done.wait(timeout=3)
        text = captured[0]
        # The selected text must reach the agent
        assert "hello world text" in text
        # Prompt should offer handling options (translate/summarize/etc)
        assert "翻译" in text or "摘要" in text or "处理" in text

    def test_prompt_injection_defense_fence(self, bridge):
        # Untrusted webpage text must be fenced + labeled as data, not instructions
        from agent_assistant.ui.bridge import TEXT_INVOKE_TEMPLATE
        assert "<<<选中文本开始" in TEXT_INVOKE_TEMPLATE
        assert "<<<选中文本结束" in TEXT_INVOKE_TEMPLATE
        # Explicit "data not instructions" label
        assert "数据" in TEXT_INVOKE_TEMPLATE or "不是对你的指令" in TEXT_INVOKE_TEMPLATE

    def test_long_text_truncated_to_cap(self, bridge):
        # Text over TEXT_INVOKE_CAP is truncated with a marker before injection
        from agent_assistant.ui.bridge import TEXT_INVOKE_CAP
        big = "y" * (TEXT_INVOKE_CAP + 5000)
        done, captured = _wait_for_agent_input_text(bridge, big)
        assert done.wait(timeout=3)
        assert "…[已截断]" in captured[0]

    def test_creates_new_conversation_titled_by_text_prefix(self, bridge):
        import agent_assistant.ui.store as store_mod

        done, _ = _wait_for_agent_input_text(bridge, "a" * 100)
        assert done.wait(timeout=3)
        convs = store_mod.ui_store.list_conversations()
        assert len(convs) == 1
        # Title = first 30 chars + ellipsis (long text truncated)
        assert convs[0]["title"].startswith("a")
        assert "…" in convs[0]["title"]
        assert bridge._active_conv_id == convs[0]["id"]

    def test_empty_text_is_noop(self, bridge):
        import agent_assistant.ui.store as store_mod

        done = threading.Event()
        captured = []

        def handler(t):
            captured.append(t)
            done.set()
            return "ok"

        bridge.set_message_handler(handler)
        bridge.invoke_on_text("")
        time.sleep(0.3)
        # No handler call, no conversation created
        assert not captured
        assert store_mod.ui_store.list_conversations() == []

    def test_status_event(self, bridge):
        done, _ = _wait_for_agent_input_text(bridge, "x")
        assert done.wait(timeout=3)
        statuses = [d for t, d in bridge.events if t == "status"]
        assert statuses and "已收到" in statuses[0]["text"]

    def test_text_invoke_event_payload(self, bridge):
        done, _ = _wait_for_agent_input_text(bridge, "the quick brown fox")
        assert done.wait(timeout=3)
        invokes = [d for t, d in bridge.events if t == "text_invoke"]
        assert len(invokes) == 1
        assert invokes[0]["text"] == "the quick brown fox"
        assert invokes[0]["conv_id"] == bridge._active_conv_id

    def test_assistant_reply_persisted(self, bridge):
        import agent_assistant.ui.store as store_mod

        done, _ = _wait_for_agent_input_text(bridge, "x")
        assert done.wait(timeout=3)
        deadline = time.time() + 3
        msgs = []
        while time.time() < deadline:
            msgs = store_mod.ui_store.list_messages(bridge._active_conv_id)
            if msgs:
                break
            time.sleep(0.02)
        assert msgs and msgs[-1]["role"] == "assistant"
        assert "已收到" in msgs[-1]["content"]

    def test_pending_text_consumed_once(self, bridge, tmp_path, monkeypatch):
        # Host writes pending_text.json when no instance is running; UI consumes
        # it on init. Mirror of pending_file_consumed_once.
        import agent_assistant.ui.bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "PENDING_FILE_DELAY", 0.0)
        monkeypatch.setattr(
            bridge_mod, "_pending_text_file", lambda: tmp_path / "pending_text.json"
        )
        done = threading.Event()
        captured = []

        def handler(t):
            captured.append(t)
            done.set()
            return "ok"

        bridge.set_message_handler(handler)
        bridge.set_pending_text("hello from pending")
        bridge._consume_pending_text()
        assert done.wait(timeout=3)
        assert "hello from pending" in captured[0]
        # Second consume is a no-op
        bridge._consume_pending_text()
        time.sleep(0.2)
        assert len(captured) == 1

    def test_consume_self_heals_crash_orphan(self, bridge, tmp_path, monkeypatch):
        # A previous crash left pending_text.json.consuming orphaned; a new
        # pending text must still be consumed (path.replace overwrites orphan).
        import agent_assistant.ui.bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "PENDING_FILE_DELAY", 0.0)
        monkeypatch.setattr(
            bridge_mod, "_pending_text_file", lambda: tmp_path / "pending_text.json"
        )
        (tmp_path / "pending_text.json.consuming").write_text(
            '{"text": "STALE"}', encoding="utf-8"
        )
        done = threading.Event()
        captured = []

        def handler(t):
            captured.append(t)
            done.set()
            return "ok"

        bridge.set_message_handler(handler)
        bridge.set_pending_text("FRESH text")
        bridge._consume_pending_text()
        assert done.wait(timeout=3)
        assert "FRESH text" in captured[0]
        assert "STALE" not in captured[0]
        # orphan cleaned up by the finally unlink
        assert not (tmp_path / "pending_text.json.consuming").exists()


# ─── report_perf (perf anomaly → dedicated '性能检测' conversation) ────────


class TestReportPerf:
    def _wait_perf_thread(self):
        for t in threading.enumerate():
            if t.name == "perf-report":
                t.join(timeout=5)

    def test_routes_to_dedicated_pinned_conversation(self, bridge):
        import agent_assistant.ui.store as store_mod

        bridge.set_message_handler(lambda text: f"诊断: {text}")
        bridge.report_perf("CPU 95%, Memory 90%")
        self._wait_perf_thread()
        perf = [c for c in store_mod.ui_store.list_conversations() if c["title"] == "性能检测"]
        assert len(perf) == 1
        assert perf[0]["pinned"] is True
        # the anomaly (user) + the diagnosis (assistant) both persisted there
        msgs = store_mod.ui_store.list_messages(perf[0]["id"])
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles
        assert any("诊断" in m["content"] for m in msgs if m["role"] == "assistant")
        # notification pushed to the (current) UI, not the analysis streamed in
        reports = [d for t, d in bridge.events if t == "perf_report"]
        assert reports and "性能报告" in reports[0]["message"]
        assert reports[0]["conv_id"] == perf[0]["id"]

    def test_anomaly_persisted_as_user_message(self, bridge):
        # Issue #2: the anomaly itself must be saved (so the log explains each diagnosis)
        import agent_assistant.ui.store as store_mod

        bridge.set_message_handler(lambda text: "ok")
        bridge.report_perf("CPU 99%")
        self._wait_perf_thread()
        perf = [c for c in store_mod.ui_store.list_conversations() if c["title"] == "性能检测"][0]
        user_msgs = [m for m in store_mod.ui_store.list_messages(perf["id"]) if m["role"] == "user"]
        assert user_msgs and "CPU 99%" in user_msgs[0]["content"]

    def test_perf_silent_true_during_agent_call(self, bridge):
        from agent_assistant.ui.bridge import api_bridge

        seen = []

        def handler(text):
            seen.append(api_bridge.perf_silent)  # True while the perf thread runs the agent
            return "ok"

        bridge.set_message_handler(handler)
        bridge.report_perf("anomaly")
        self._wait_perf_thread()
        assert seen == [True]
        assert api_bridge.perf_silent is False  # cleared after the run

    def test_perf_silent_false_in_other_thread(self, bridge):
        # Scoped to the perf thread only — a different thread is not silenced
        from agent_assistant.ui.bridge import api_bridge

        seen = []

        def check():
            seen.append(api_bridge.perf_silent)

        t = threading.Thread(target=check, name="not-perf")
        t.start()
        t.join()
        assert seen == [False]

    def test_reuses_existing_performance_conversation(self, bridge):
        import agent_assistant.ui.store as store_mod

        bridge.set_message_handler(lambda text: "ok")
        bridge.report_perf("first anomaly")
        self._wait_perf_thread()
        first = [c for c in store_mod.ui_store.list_conversations() if c["title"] == "性能检测"][0]
        bridge.report_perf("second anomaly")
        self._wait_perf_thread()
        perf = [c for c in store_mod.ui_store.list_conversations() if c["title"] == "性能检测"]
        assert len(perf) == 1  # not duplicated — find_or_create reused it
        assert perf[0]["id"] == first["id"]
        msgs = store_mod.ui_store.list_messages(first["id"])
        assert sum(1 for m in msgs if m["role"] == "user") == 2  # both anomalies logged
