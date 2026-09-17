"""Tool archive + recall_tool_result: persistence, retrieval, loop integration.

Pins the contract: every main-loop tool call lands in a per-conversation
JSONL archive (full args + full result), and recall_tool_result fetches
records back — by exact call_id, by tool/keyword search, paginated for long
payloads, current session only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_assistant.tools.tool_archive import ToolArchive


@pytest.fixture()
def archive(tmp_path: Path) -> ToolArchive:
    return ToolArchive(base_dir=tmp_path / "tool_archive")


def _record(call_id: str, *, tool: str = "read_page", ok: bool = True) -> dict:
    return {
        "ok": ok,
        "data": {"content": f"payload for {call_id}", "chars": 42},
    }


class TestRecordAndFetch:
    def test_roundtrip(self, archive: ToolArchive):
        archive.record(
            conversation_id="conv1",
            call_id="call_a1",
            tool="read_page",
            arguments='{"url": "https://example.com"}',
            result=_record("call_a1"),
        )
        rec = archive.get("conv1", "call_a1")
        assert rec is not None
        assert rec["tool"] == "read_page"
        assert rec["ok"] is True
        assert rec["result"]["data"]["content"] == "payload for call_a1"
        assert rec["session_id"] == "conv1"
        assert rec["id"].startswith("T-")
        assert "ts" in rec and "T" in rec["ts"]

    def test_conversation_isolation(self, archive: ToolArchive):
        archive.record(
            conversation_id="conv1", call_id="call_a1", tool="t",
            arguments="{}", result={"ok": True},
        )
        assert archive.get("conv2", "call_a1") is None
        assert archive.get("conv1", "call_a1") is not None

    def test_missing_call_returns_none(self, archive: ToolArchive):
        assert archive.get("conv1", "nope") is None

    def test_record_never_raises(self, tmp_path: Path):
        # A path that cannot exist (a file used as a directory) must be
        # swallowed by record() — archiving may never break tool execution.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        archive = ToolArchive(base_dir=blocker / "nested")
        archive.record(
            conversation_id="c", call_id="call_x", tool="t",
            arguments="{}", result={"ok": True},
        )  # no raise


class TestSearch:
    @pytest.fixture(autouse=True)
    def _seed(self, archive: ToolArchive):
        self.archive = archive
        for i, (tool, args) in enumerate([
            ("read_page", '{"url": "https://a.com/x"}'),
            ("web_search", '{"query": "夜曲 周杰伦"}'),
            ("ui_inspect", '{"title_pattern": "网易云"}'),
            ("read_page", '{"url": "https://b.com/y"}'),
        ]):
            archive.record(
                conversation_id="c",
                call_id=f"call_{i}",
                tool=tool,
                arguments=args,
                result={"ok": True, "data": {"n": i}},
            )

    def test_by_tool_most_recent_first(self):
        hits = self.archive.search("c", tool="read_page")
        assert [h["call_id"] for h in hits] == ["call_3", "call_0"]

    def test_by_keyword_matches_args_and_result(self):
        assert [h["call_id"] for h in self.archive.search("c", keyword="夜曲")] == [
            "call_1"
        ]
        # keyword found inside the result payload
        hits = self.archive.search("c", keyword='"n": 2')
        assert [h["call_id"] for h in hits] == ["call_2"]

    def test_no_match_empty(self):
        assert self.archive.search("c", keyword="不存在") == []

    def test_limit(self):
        assert len(self.archive.search("c", limit=2)) == 2

    def test_recent_when_no_filters(self):
        hits = self.archive.search("c")
        assert [h["call_id"] for h in hits] == ["call_3", "call_2", "call_1", "call_0"]


class TestRotationAndRetention:
    def test_rotation_keeps_records_readable(self, tmp_path: Path):
        archive = ToolArchive(base_dir=tmp_path / "ta", max_file_bytes=200)
        for i in range(10):
            archive.record(
                conversation_id="c", call_id=f"call_{i}", tool="t",
                arguments="{}", result={"ok": True, "i": i},
            )
        files = list((tmp_path / "ta" / "c").glob("tool_details*.jsonl"))
        assert len(files) >= 2  # rotated at least once
        # every record still retrievable across rotated files
        rec = archive.get("c", "call_0")
        assert rec is not None and rec["result"]["i"] == 0
        assert len(archive.search("c", limit=10)) == 10

    def test_expired_files_deleted_on_write(self, tmp_path: Path):
        archive = ToolArchive(base_dir=tmp_path / "ta", retention_days=7)
        archive.record(
            conversation_id="old", call_id="call_old", tool="t",
            arguments="{}", result={"ok": True},
        )
        old_file = next((tmp_path / "ta" / "old").glob("tool_details*.jsonl"))
        expired = time.time() - 8 * 86400
        os.utime(old_file, (expired, expired))
        archive._last_cleanup = 0.0  # force cleanup on next write
        archive.record(
            conversation_id="fresh", call_id="call_new", tool="t",
            arguments="{}", result={"ok": True},
        )
        assert not old_file.exists()


class TestRecallTool:
    @pytest.fixture(autouse=True)
    def _setup(self, archive: ToolArchive, monkeypatch):
        import agent_assistant.tools.tool_archive as mod

        self.archive = archive
        monkeypatch.setattr(mod, "tool_archive", archive)
        from agent_assistant.tools.tool_archive import RecallToolResultTool

        self.tool = RecallToolResultTool()

    def _seed_one(self, call_id: str = "call_big") -> None:
        self.archive.record(
            conversation_id="local",
            call_id=call_id,
            tool="ui_inspect",
            arguments='{"title_pattern": "网易云"}',
            result={"ok": True, "data": {"controls": [{"name": "播放"}]}},
        )

    def test_schema_registered_shape(self):
        schema = self.tool.to_openai_schema()
        fn = schema["function"]
        assert fn["name"] == "recall_tool_result"
        assert fn["parameters"]["required"] == []

    def test_fetch_by_call_id(self):
        self._seed_one()
        out = self.tool.execute(call_id="call_big")
        assert out.ok
        data = out.data
        assert data["mode"] == "fetch"
        assert data["tool"] == "ui_inspect"
        assert json.loads(data["result"])["data"]["controls"][0]["name"] == "播放"

    def test_fetch_unknown_call_id_fails(self):
        out = self.tool.execute(call_id="call_missing")
        assert not out.ok
        assert out.error_category == "not_found"

    def test_search_by_tool_name(self):
        self._seed_one("call_r1")
        self.archive.record(
            conversation_id="local", call_id="call_r2", tool="web_search",
            arguments='{"query": "x"}', result={"ok": True},
        )
        out = self.tool.execute(tool_name="web_search")
        assert out.ok
        assert [m["call_id"] for m in out.data["matches"]] == ["call_r2"]

    def test_no_args_lists_recent(self):
        self._seed_one("call_recent")
        out = self.tool.execute()
        assert out.ok
        assert out.data["matches"][0]["call_id"] == "call_recent"
        assert "result_preview" in out.data["matches"][0]

    def test_pagination(self, archive: ToolArchive):
        big = {"ok": True, "data": {"blob": "x" * 30_000}}
        archive.record(
            conversation_id="local", call_id="call_huge", tool="read_page",
            arguments="{}", result=big,
        )
        page1 = self.tool.execute(call_id="call_huge")
        assert page1.ok and page1.data["truncated"] is True
        assert page1.data["result_total_chars"] > 30_000
        hint_offset = int(page1.data["hint"].split("offset=")[1].rstrip(")."))
        page2 = self.tool.execute(call_id="call_huge", offset=hint_offset)
        assert page2.ok
        assert page2.data["result_offset"] == hint_offset
        # assembled pages reconstruct the full JSON
        full = page1.data["result"] + page2.data["result"]
        assert json.loads(full) == big

    def test_scoped_to_current_conversation(self, archive: ToolArchive, monkeypatch):
        archive.record(
            conversation_id="other-conv", call_id="call_o", tool="t",
            arguments="{}", result={"ok": True},
        )
        import threading

        from agent_assistant.subagents.context import AgentExecutionContext, bind_execution_context

        ctx = AgentExecutionContext(
            conversation_id="my-conv",
            parent_turn_id="turn1",
            owner_id="o",
            cancel_event=threading.Event(),
        )
        with bind_execution_context(ctx):
            out = self.tool.execute(tool_name="t")
        assert not out.ok  # other-conv's record invisible


class TestLoopIntegration:
    """The main loop archives every executed tool call."""

    @pytest.mark.asyncio
    async def test_loop_archives_tool_results(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        from agent_assistant.agent.loop import AgentLoop
        from agent_assistant.memory.manager import MemoryManager
        from agent_assistant.tools import tool_registry
        from agent_assistant.tools.base import Tool, ToolResult

        class EchoTool(Tool):
            name = "echo_archive_probe"
            description = "echo test tool"
            parameters = []

            def execute(self, **kwargs):
                return ToolResult.success({"echo": "hi " * 50})

        tool_registry.register(EchoTool())

        archive = ToolArchive(base_dir=tmp_path / "ta")
        monkeypatch.setattr("agent_assistant.agent.loop.tool_archive", archive)

        class FakeChunk:
            def __init__(self, content: str = "", tool_calls=None):
                self.content = content
                self.tool_calls = tool_calls or []
                self.reasoning_content = None

            @property
            def choices(self):
                return [self]

            @property
            def delta(self):
                return self

        class FakeStream:
            def __init__(self, chunks):
                self._chunks = chunks

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._chunks:
                    raise StopAsyncIteration
                return self._chunks.pop(0)

        class FakeDelta:
            def __init__(self, index, id, name):
                self.index = index
                self.id = id
                self.type = "function"
                self.function = type("F", (), {"name": name, "arguments": "{}"})()

        calls = {"n": 0}

        class FakeLLM:
            async def achat_stream(self, messages, tools=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return FakeStream([
                        FakeChunk(tool_calls=[FakeDelta(0, "call_e1", "echo_archive_probe")])
                    ])
                return FakeStream([FakeChunk(content="done")])

        monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM())

        class FakeMemoryService:
            def build_profile_additions(self) -> str:
                return ""

            def build_study_profile_section(self) -> str:
                return ""

        monkeypatch.setattr(
            "agent_assistant.agent.loop.memory_service", FakeMemoryService()
        )

        loop = AgentLoop(
            memory_manager=MemoryManager(
                token_budget=100_000,
                summary_cap=1200,
                summarizer=lambda t: "s",
            )
        )
        result = await loop.achat("跑一下 echo")
        assert result == "done"

        rec = archive.get(loop.conversation_id, "call_e1")
        assert rec is not None
        assert rec["tool"] == "echo_archive_probe"
        assert rec["ok"] is True
        assert json.loads(rec["arguments"]) == {}
        assert rec["result"]["data"]["echo"].startswith("hi ")

        # failure results are archived too (ok=false path)
        class FailTool(Tool):
            name = "fail_archive_probe"
            description = "always fails"
            parameters = []

            def execute(self, **kwargs):
                return ToolResult.failure("boom", code=500)

        tool_registry.register(FailTool())
        calls["n"] = 0

        class FakeLLM2(FakeLLM):
            async def achat_stream(self, messages, tools=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    return FakeStream([
                        FakeChunk(tool_calls=[FakeDelta(0, "call_e2", "fail_archive_probe")])
                    ])
                return FakeStream([FakeChunk(content="done")])

        monkeypatch.setattr("agent_assistant.agent.loop.llm_client", FakeLLM2())
        await loop.achat("再试失败工具")
        rec2 = archive.get(loop.conversation_id, "call_e2")
        assert rec2 is not None
        assert rec2["ok"] is False
