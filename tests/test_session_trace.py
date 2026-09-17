"""Session trace (阶段 4): events.jsonl + tool_details.jsonl.

Covers the two files, their split of responsibility, and the埋点 that feeds
them:
  * the writer's contract (never raises, id sequence, rotation, spill)
  * the event stream must NOT duplicate tool bodies (that's tool_details' job)
  * the loop emits turn/user/llm/tool events; manager emits compaction/pruning
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_assistant.memory.manager import MemoryManager
from agent_assistant.memory.session_trace import (
    EVENT_COMPACTION,
    EVENT_LLM,
    EVENT_PRUNING,
    EVENT_TOOL,
    EVENT_TURN_END,
    EVENT_TURN_START,
    EVENT_USER,
    SessionTrace,
    safe_session_name,
)


@pytest.fixture
def trace(tmp_path: Path) -> SessionTrace:
    return SessionTrace(base_dir=tmp_path / "sessions")


# ── writer contract ───────────────────────────────────────────────────────


class TestEventWriter:
    def test_roundtrip_and_required_fields(self, trace: SessionTrace) -> None:
        trace.event(
            "conv1", EVENT_TOOL, turn=3, ok=True, summary="读了 a.py，10 行",
            detail_ref="T-0001", tool="read_file",
        )
        events = trace.iter_events("conv1")
        assert len(events) == 1
        e = events[0]
        assert e["event"] == "tool"
        assert e["session_id"] == "conv1"
        assert e["turn"] == 3
        assert e["ok"] is True
        assert e["error_category"] is None  # always present, machine-readable
        assert e["summary"] == "读了 a.py，10 行"
        assert e["detail_ref"] == "T-0001"
        assert e["v"] == 1 and "ts" in e

    def test_failure_category_is_recorded_not_prose(self, trace: SessionTrace) -> None:
        trace.event(
            "c", EVENT_TOOL, ok=False, error_category="timeout",
            summary="读 https://x 失败：timeout",
        )
        e = trace.iter_events("c")[0]
        assert e["ok"] is False
        assert e["error_category"] == "timeout"

    def test_summary_is_single_line_and_bounded(self, trace: SessionTrace) -> None:
        trace.event("c", EVENT_LLM, summary="line1\nline2" + "x" * 900)
        s = trace.iter_events("c")[0]["summary"]
        assert "\n" not in s
        assert len(s) <= 400

    def test_extra_payload_is_bounded(self, trace: SessionTrace) -> None:
        trace.event("c", EVENT_COMPACTION, summary="x", huge="y" * 50_000)
        e = trace.iter_events("c")[0]
        assert e["extra"]["_truncated"] is True
        assert len(e["extra"]["_preview"]) < 2100

    def test_write_failure_never_raises(self, tmp_path: Path) -> None:
        """A blocker file in place of the dir must not break the caller."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        trace = SessionTrace(base_dir=blocker / "sessions")
        trace.event("c", EVENT_USER, summary="hi")  # must not raise
        assert trace.iter_events("c") == []

    def test_sessions_are_isolated(self, trace: SessionTrace) -> None:
        trace.event("a", EVENT_USER, summary="A")
        trace.event("b", EVENT_USER, summary="B")
        assert [e["summary"] for e in trace.iter_events("a")] == ["A"]
        assert [e["summary"] for e in trace.iter_events("b")] == ["B"]

    def test_unsafe_session_id_is_sanitized(self, trace: SessionTrace) -> None:
        weird = "../../etc/passwd"
        trace.event(weird, EVENT_USER, summary="x")
        # Nothing escaped the base dir; the file lives under a mangled name.
        written = list((trace.base_dir).glob("*/events.jsonl"))
        assert len(written) == 1
        assert ".." not in written[0].as_posix()
        assert [e["summary"] for e in trace.iter_events(weird)] == ["x"]
        assert safe_session_name(weird) == written[0].parent.name


class TestToolDetails:
    def test_id_sequence_and_content(self, trace: SessionTrace) -> None:
        id1 = trace.tool_detail(
            "c", call_id="call_a", tool="read_file",
            arguments={"path": "a.py"}, ok=True, result={"ok": True, "data": {}},
        )
        id2 = trace.tool_detail(
            "c", call_id="call_b", tool="read_file",
            arguments={"path": "b.py"}, ok=True, result={"ok": True, "data": {}},
        )
        assert id1 == "T-0001" and id2 == "T-0002"
        recs = trace.iter_tool_details("c")
        assert [r["id"] for r in recs] == ["T-0001", "T-0002"]
        assert recs[0]["call_id"] == "call_a"

    def test_ids_survive_a_new_writer_instance(self, tmp_path: Path) -> None:
        """A restart must not restart the id sequence (ids stay unique)."""
        t1 = SessionTrace(base_dir=tmp_path / "s")
        t1.tool_detail("c", call_id="a", tool="t", arguments="{}", ok=True,
                       result={"ok": True})
        t2 = SessionTrace(base_dir=tmp_path / "s")
        assert t2.tool_detail("c", call_id="b", tool="t", arguments="{}",
                              ok=True, result={"ok": True}) == "T-0002"

    def test_oversized_result_spills_to_own_file(self, tmp_path: Path) -> None:
        trace = SessionTrace(base_dir=tmp_path / "s")
        big = {"ok": True, "data": {"content": "z" * (1024 * 1024 + 100)}}
        detail_id = trace.tool_detail(
            "c", call_id="a", tool="read_page", arguments="{}", ok=True, result=big,
        )
        rec = trace.iter_tool_details("c")[0]
        assert rec["result"]["_ref"] == f"details/{detail_id}.json"
        assert rec["result"]["_bytes"] > 1024 * 1024
        # The spilled file holds the real payload and is path-guarded.
        text = trace.read_detail_file("c", rec["result"]["_ref"])
        assert text is not None and "z" * 100 in text
        assert trace.read_detail_file("c", "../../secret") is None

    def test_events_do_not_duplicate_tool_bodies(self, trace: SessionTrace) -> None:
        """The stream carries the one-line summary, never the payload."""
        trace.event("c", EVENT_TOOL, ok=True, summary="读了 big.txt，500 行")
        raw = (trace.session_dir("c") / "events.jsonl").read_text(encoding="utf-8")
        assert "读了 big.txt，500 行" in raw
        assert "content" not in raw


# ── manager 埋点 ──────────────────────────────────────────────────────────


class TestManagerEmitsCompaction:
    def _mgr(self, trace: SessionTrace, cap: int = 200) -> MemoryManager:
        mgr = MemoryManager(
            token_budget=cap,
            summary_cap=5000,
            summarizer=lambda t: "SUMMARY(" + t[:30] + ")",
        )
        mgr.attach_trace(trace, "conv-x")
        return mgr

    def test_compaction_event_records_trigger_and_size(
        self, trace: SessionTrace
    ) -> None:
        mgr = self._mgr(trace)
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old " + "a" * 3000},
            {"role": "assistant", "content": "old answer " + "b" * 3000},
            {"role": "user", "content": "new task"},
        ]
        mgr.maybe_compact(msgs)
        events = [e for e in trace.iter_events("conv-x")
                  if e["event"] == EVENT_COMPACTION]
        assert len(events) == 1
        e = events[0]
        assert e["ok"] is True
        assert e["extra"]["dropped"] > 0
        assert e["tokens"]["window_before"] > e["tokens"]["budget"]

    def test_pruning_event_on_result_summarization(
        self, trace: SessionTrace
    ) -> None:
        # 预算要"装得下最近几条、装不下全部"，否则 token 保护线不生效
        # （旧实现按条数判定，用多大预算都能触发；现在不行了）
        mgr = self._mgr(trace, cap=2_000)
        msgs: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
        for i in range(8):
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"t{i}", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
            ]})
            msgs.append({"role": "tool", "tool_call_id": f"t{i}",
                         "content": '{"ok": true, "data": {"content": "'
                                    + "x" * 800 + '"}}'})
        mgr.maybe_compact(msgs)
        events = [e for e in trace.iter_events("conv-x")
                  if e["event"] == EVENT_PRUNING]
        assert events, "pruning must be auditable — otherwise 'why is this a " \
                       "one-liner?' has no answer"
        assert events[0]["extra"]["results_summarized"] > 0

    def test_summary_archive_event_when_layers_overflow(
        self, trace: SessionTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """摘要层超限归档也要留痕 —— 否则「这段摘要为什么没了」无解。"""
        monkeypatch.setattr("agent_assistant.config.settings.data_dir", tmp_path)
        mgr = MemoryManager(
            token_budget=120,
            summary_cap=60,  # 极小：逼出 _fit_within_cap 的归档分支
            summarizer=lambda t: t,  # identity —— 层不会被压小
        )
        mgr.attach_trace(trace, "conv-arch")
        for i in range(3):
            mgr.maybe_compact([
                {"role": "system", "content": "sys"},
                {"role": "user", "content": f"task{i} " + "a" * 2000},
                {"role": "assistant", "content": "answer " + "b" * 2000},
                {"role": "user", "content": f"anchor{i}"},
            ])
        events = [e for e in trace.iter_events("conv-arch")
                  if e["event"] == "summary_archive"]
        assert events, "归档摘要层必须可审计"
        assert events[0]["extra"]["archived_layers"] >= 1
        assert events[0]["extra"]["files"]

    def test_no_trace_attached_writes_nothing(self, tmp_path: Path) -> None:
        """A bare manager (as in most tests) must stay I/O-free."""
        mgr = MemoryManager(
            token_budget=100, summary_cap=500,
            summarizer=lambda t: "S",
        )
        mgr.maybe_compact([
            {"role": "system", "content": "s"},
            {"role": "user", "content": "x" * 4000},
            {"role": "assistant", "content": "y" * 4000},
            {"role": "user", "content": "now"},
        ])
        assert not list((tmp_path / "sessions").glob("**/events.jsonl"))


# ── loop 埋点 ─────────────────────────────────────────────────────────────


class _FakeToolCallDelta:
    def __init__(self, *, index: int, id: str, name: str, arguments: str = "{}"):
        self.index = index
        self.id = id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


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


def _register_noop_tool() -> None:
    from agent_assistant.tools import tool_registry
    from agent_assistant.tools.base import Tool, ToolResult

    class TraceNoopTool(Tool):
        name = "trace_noop"
        description = "test no-op"
        parameters = []

        def execute(self, **kwargs):
            return ToolResult.success({"echo": "hi"})

    tool_registry.register(TraceNoopTool())


class TestLoopEmitsEvents:
    @pytest.mark.asyncio
    async def test_turn_user_tool_llm_events(
        self, monkeypatch: pytest.MonkeyPatch, trace: SessionTrace
    ) -> None:
        _register_noop_tool()
        calls = {"n": 0}

        class FakeLLM:
            model = "fake-model"

            async def achat_stream(self, messages, tools=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _FakeStream([_FakeChunk(tool_calls=[
                        _FakeToolCallDelta(index=0, id="call_1", name="trace_noop")
                    ])])
                return _FakeStream([_FakeChunk(content="DONE")])

        monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())
        monkeypatch.setattr("agent_assistant.agent.loop.session_trace", trace)
        # Both files must land in the SAME session directory (that's the point
        # of the layout) — conftest otherwise parks the archive elsewhere.
        from agent_assistant.tools.tool_archive import ToolArchive

        monkeypatch.setattr(
            "agent_assistant.agent.loop.tool_archive",
            ToolArchive(base_dir=trace.base_dir),
        )
        monkeypatch.setattr(
            "agent_assistant.agent.loop.AgentLoop._refresh_system_prompt",
            lambda self: None,
        )

        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(conversation_id="conv-loop")
        reply = await loop.achat("帮我看看这个文件")

        assert reply == "DONE"
        events = trace.iter_events("conv-loop")
        kinds = [e["event"] for e in events]
        assert kinds[0] == EVENT_TURN_START
        assert kinds[-1] == EVENT_TURN_END
        assert EVENT_USER in kinds and EVENT_TOOL in kinds and EVENT_LLM in kinds
        assert all(e["turn"] == 1 for e in events)

        tool_ev = next(e for e in events if e["event"] == EVENT_TOOL)
        assert tool_ev["ok"] is True
        # Tool name/call id are event-specific → they live in `extra`, keeping
        # the top level to the universal fields (ts/turn/ok/summary/...).
        assert tool_ev["extra"]["tool"] == "trace_noop"
        # The pointer ties the event to the full payload in tool_details.
        assert tool_ev["detail_ref"].startswith("T-")
        # tool_details holds what the event deliberately does not.
        details = trace.iter_tool_details("conv-loop")
        assert details[0]["id"] == tool_ev["detail_ref"]
        assert details[0]["tool"] == "trace_noop"

    @pytest.mark.asyncio
    async def test_turn_end_marks_failure_on_exception(
        self, monkeypatch: pytest.MonkeyPatch, trace: SessionTrace
    ) -> None:
        class BoomLLM:
            model = "fake-model"

            async def achat_stream(self, messages, tools=None, **kw):
                raise RuntimeError("upstream down")

        monkeypatch.setattr("agent_assistant.agent.loop.llm_client", BoomLLM())
        monkeypatch.setattr("agent_assistant.agent.loop.session_trace", trace)
        monkeypatch.setattr(
            "agent_assistant.agent.loop.AgentLoop._refresh_system_prompt",
            lambda self: None,
        )
        monkeypatch.setattr(
            "agent_assistant.agent.loop.public_llm_error", lambda exc: "模型服务不可用"
        )

        from agent_assistant.agent.loop import AgentLoop

        loop = AgentLoop(conversation_id="conv-fail")
        with pytest.raises(RuntimeError):
            await loop.achat("hi")

        events = trace.iter_events("conv-fail")
        assert any(e["event"] == "llm_error" and e["ok"] is False for e in events)
        tail = events[-1]
        assert tail["event"] == EVENT_TURN_END and tail["ok"] is False
        assert tail["error_category"] == "RuntimeError"


class TestRecallIsAudited:
    def test_recall_tool_result_writes_event(self, trace: SessionTrace) -> None:
        import agent_assistant.memory.session_trace as st_mod
        import agent_assistant.tools.tool_archive as ta_mod
        from agent_assistant.tools.tool_archive import RecallToolResultTool

        archive = ta_mod.ToolArchive(base_dir=trace.base_dir)
        archive.record(
            conversation_id="local", call_id="call_x", tool="read_file",
            arguments="{}", result={"ok": True, "data": {"a": 1}},
        )
        original_trace, original_archive = st_mod.session_trace, ta_mod.tool_archive
        st_mod.session_trace = trace
        ta_mod.tool_archive = archive
        try:
            result = RecallToolResultTool().execute(call_id="call_x")
        finally:
            st_mod.session_trace = original_trace
            ta_mod.tool_archive = original_archive

        assert result.ok
        events = [e for e in trace.iter_events("local") if e["event"] == "recall"]
        assert events and events[0]["ok"] is True
        assert "call_x" in events[0]["summary"]
