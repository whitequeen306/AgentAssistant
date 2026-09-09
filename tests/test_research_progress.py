"""Sub-agent progress UI events + leaked tool-call markup report retry."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_assistant.config import settings
from agent_assistant.tools import research
from agent_assistant.tools.base import ToolResult

import pytest


def _final_response(text: str = "final report"):
    msg = SimpleNamespace(tool_calls=None, content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _tool_call_response(name: str, arguments: str, call_id: str = "call_1"):
    tc = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


class TestLeakedMarkupDetection:
    def test_dsml_block_detected(self):
        leaked = "前几轮检索：\n<|DSML|><tool_calls><invoke name=\"web_search\">"
        assert research._looks_like_leaked_tool_markup(leaked)

    def test_invoke_parameter_markup_detected(self):
        assert research._looks_like_leaked_tool_markup(
            '<invoke name="x"><parameter name="q">y</parameter></invoke>'
        )

    def test_normal_report_not_flagged(self):
        assert not research._looks_like_leaked_tool_markup(
            "# 调研报告\n已确认院校清单如下……"
        )
        assert not research._looks_like_leaked_tool_markup("")


class TestLeakedMarkupRetry:
    def test_markup_final_answer_retried_and_replaced(self, tmp_data_dir):
        """First final answer is raw DSML markup → one no-tools retry whose
        clean text becomes the saved report."""
        leaked = '<|DSML|><invoke name="web_search"><parameter name="query">x</parameter>'
        responses = iter([
            _final_response(leaked),
            _final_response("# 报告\n干净的正文"),
        ])
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            return next(responses)

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert calls["n"] == 2
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "DSML" not in report
        assert "干净的正文" in report
        trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "report_retry" in trace

    def test_retry_still_bad_falls_back_to_findings_report(self, tmp_data_dir):
        """Retry also returns markup → deterministic findings report, never
        leaked markup in report.md."""
        leaked = '<|DSML|><invoke name="web_search"><parameter name="query">x</parameter>'
        responses = iter([_final_response(leaked), _final_response(leaked)])
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            return next(responses)

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert calls["n"] == 2, "exactly one retry, no loop"
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        assert "DSML" not in report, "report.md must never contain leaked markup"
        assert "自动整理" in report  # deterministic findings report marker
        trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "report_fallback" in trace

    def test_retry_uses_compact_context(self, tmp_data_dir):
        """The retry must NOT reuse the full tool-call-saturated history —
        compact context (goal + findings digest) breaks the markup pattern."""
        leaked = '<|DSML|><invoke name="web_search"><parameter name="query">x</parameter>'
        responses = iter([_final_response(leaked), _final_response("# 报告\n干净正文")])
        captured: list[list[dict]] = []

        def chat_side_effect(messages, tools):
            captured.append(list(messages))
            return next(responses)

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        retry_messages = captured[1]
        assert len(retry_messages) == 2, "compact context = system + user only"
        assert retry_messages[0]["role"] == "system"
        assert retry_messages[1]["role"] == "user"
        assert "goal" in retry_messages[1]["content"]
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        assert "干净正文" in (run_dir / "report.md").read_text(encoding="utf-8")

    def test_clean_final_answer_no_retry(self, tmp_data_dir):
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            return _final_response("# 报告\n正常内容")

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert calls["n"] == 1


class TestFailureSalvageContract:
    """Every mid-run failure path must hand the main agent partial findings +
    resume_from — never a bare error that strands the task."""

    def _seeded_run(self, tmp_data_dir):
        """A run that gathered one finding before dying."""
        responses = iter([
            _tool_call_response("web_search", '{"query": "东大2026目录"}', "c1"),
            RuntimeError("boom"),  # llm_err path on second call
        ])
        search_result = ToolResult.success(
            data={"results": [{"title": "东大2026目录", "url": "http://yz.neu.edu.cn/a"}]}
        )
        return responses, search_result

    def test_generic_llm_error_salvages(self, tmp_data_dir):
        responses = iter([
            _tool_call_response("web_search", '{"query": "东大2026目录"}', "c1"),
        ])
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_response("web_search", '{"query": "东大2026目录"}', "c1")
            raise RuntimeError("connection reset")

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=ToolResult.success(
                data={"results": [{"title": "东大2026目录", "url": "http://yz.neu.edu.cn/a"}]}
            ),
        ):
            result = research.run_research_subagent("goal", max_turns=10)

        assert not result.ok
        assert result.error_category == "subagent_timeout"
        assert result.data is not None
        assert result.data["resume_from"]
        assert result.data["findings_count"] >= 1
        assert "东大2026目录" in result.data["summary"]
        # Recovery instruction is in the error string itself (models read
        # error first), with a ready-made next_action call.
        assert "RECOVER YOURSELF" in result.error
        assert result.data["resume_from"] in result.error
        na = result.data["next_action"]
        assert na["tool"] == "dispatch_research"
        assert na["args"]["resume_from"] == result.data["resume_from"]
        assert na["args"]["goal"] == "goal"

    def test_empty_response_salvages(self, tmp_data_dir):
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_response("web_search", '{"query": "q"}', "c1")
            return SimpleNamespace(choices=[])  # empty → response None path?

        # choices=[] would IndexError; simulate response=None via guarded call
        with patch.object(
            research, "_call_llm_guarded",
            side_effect=[
                (_tool_call_response("web_search", '{"query": "q"}', "c1"), None),
                (None, None),  # empty response path
            ],
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=ToolResult.success(
                data={"results": [{"title": "T", "url": "http://a.cn/1"}]}
            ),
        ):
            result = research.run_research_subagent("goal", max_turns=10)

        assert not result.ok
        assert result.error_category == "subagent_timeout"
        assert result.data is not None and result.data["resume_from"]
        assert "RECOVER YOURSELF" in result.error
        assert result.data["next_action"]["args"]["resume_from"] == (
            result.data["resume_from"]
        )


class TestSubagentContextManagement:
    def test_cap_tool_content_truncates(self):
        long_content = "x" * 7000
        out = research._cap_tool_content(long_content)
        assert len(out) < 7000
        assert "truncated" in out
        assert research._cap_tool_content("short") == "short"

    def test_age_off_stubs_old_keeps_recent(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "g"},
        ]
        summaries = {}
        for i in range(12):
            tc = f"c{i}"
            messages.append({"role": "assistant", "tool_calls": [], "content": ""})
            messages.append({
                "role": "tool",
                "tool_call_id": tc,
                "content": f"full page body {i} " + "y" * 3000,
            })
            summaries[tc] = f"摘要{i}"
        stubbed: set[str] = set()
        research._age_off_tool_results(messages, summaries, stubbed)

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        # 12 tool messages, 8 recent kept full, 4 oldest stubbed
        for m in tool_msgs[:4]:
            data = json.loads(m["content"])
            assert data["stale"] is True
            assert "摘要" in data["summary"]
        for m in tool_msgs[4:]:
            assert m["content"].startswith("full page body")
        # No helper keys leak into the API payload
        assert all("_stubbed" not in m for m in messages)
        # Idempotent: second pass doesn't re-stub or change anything
        before = [m["content"] for m in tool_msgs]
        research._age_off_tool_results(messages, summaries, stubbed)
        assert [m["content"] for m in tool_msgs] == before

    def test_full_tool_output_archived_to_raw_jsonl(self, tmp_data_dir):
        """Nothing is lost: every tool's FULL output lands in raw.jsonl even
        though the in-context copy gets capped/stubbed."""
        body = "网页正文" * 2000  # 8k chars — exceeds the in-context cap
        responses = iter([
            _tool_call_response("read_page", '{"url": "https://a.cn/x"}', "c1"),
            _final_response("report"),
        ])
        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute",
            return_value=ToolResult.success(data={"content": body}),
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        raw = (run_dir / "raw.jsonl").read_text(encoding="utf-8")
        assert body in raw, "full tool output must be archived in raw.jsonl"
        assert result.data["l0_raw"].endswith("raw.jsonl")

    def test_long_run_context_stays_bounded(self, tmp_data_dir):
        """Across many turns, the messages sent to the LLM must not grow
        linearly with page bodies — old ones collapse to summaries."""
        captured_sizes: list[int] = []
        body = "网页正文" * 2000  # ~8k chars per read

        def chat_side_effect(messages, tools):
            captured_sizes.append(sum(len(str(m.get("content") or "")) for m in messages))
            if tools is None:
                return _final_response("report")
            return _tool_call_response("read_page", '{"url": "https://a.cn/x"}', f"c{len(captured_sizes)}")

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute",
            return_value=ToolResult.success(data={"content": body}),
        ):
            result = research.run_research_subagent("goal", max_turns=15)

        assert result.ok
        # Without aging, turn-15 context would carry 14 × 8k ≈ 112k chars of
        # page bodies. With aging (8 recent full + stubs), far less.
        assert captured_sizes[-1] < 80000, (
            f"context grew unbounded: {captured_sizes[-1]} chars"
        )


class TestInlineReport:
    def test_full_report_returned_in_data(self, tmp_data_dir):
        body = "# 调研报告\n" + "正文内容。" * 100
        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response(body),
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert result.data["report"] == body
        assert result.data["report_truncated"] is False

    def test_oversized_report_capped_and_flagged(self, tmp_data_dir):
        body = "长" * 20000
        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response(body),
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert len(result.data["report"]) == 12000
        assert result.data["report_truncated"] is True
        # Full text still persisted to disk
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        assert len((run_dir / "report.md").read_text(encoding="utf-8")) == 20000


class TestProgressEvents:
    def test_progress_label_formats(self):
        assert research._progress_label(
            "web_search", {"query": "东北大学2026招生目录"}
        ) == "搜索：东北大学2026招生目录"
        assert research._progress_label(
            "read_page", {"url": "https://yz.neu.edu.cn/a/b"}
        ) == "阅读页面：yz.neu.edu.cn"
        assert research._progress_label(
            "extract_content", {"url": "https://x.edu.cn/", "selector": "a"}
        ) == "提取内容：x.edu.cn"
        assert research._progress_label("save_note", {"title": "t"}) == "保存笔记"

    def test_progress_events_pushed_during_run(self, tmp_data_dir):
        events: list[dict] = []

        class FakeBridge:
            report_silent = False

            def push_event(self, t, data):
                events.append({"type": t, **data})

        responses = iter([
            _tool_call_response("web_search", '{"query": "东北大学2026招生目录"}', "c1"),
            _final_response("done"),
        ])
        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=ToolResult.success(data={"results": []}),
        ), patch(
            "agent_assistant.ui.bridge.api_bridge", FakeBridge()
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        progress = [e for e in events if e["type"] == "subagent_progress"]
        assert len(progress) == 1
        assert progress[0]["tool"] == "web_search"
        assert progress[0]["label"] == "搜索：东北大学2026招生目录"
        assert progress[0]["turn"] == 1
        assert progress[0]["max_turns"] == 5

    def test_progress_suppressed_in_background_report(self, tmp_data_dir):
        events: list[dict] = []

        class FakeBridge:
            report_silent = True  # morning-briefing / perf report thread

            def push_event(self, t, data):
                events.append({"type": t, **data})

        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("done"),
        ), patch(
            "agent_assistant.ui.bridge.api_bridge", FakeBridge()
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert [e for e in events if e["type"] == "subagent_progress"] == []

    def test_wrapup_notice_emits_progress(self, tmp_data_dir):
        events: list[dict] = []

        class FakeBridge:
            report_silent = False

            def push_event(self, t, data):
                events.append({"type": t, **data})

        def chat_side_effect(messages, tools):
            if tools is None:
                return _final_response("forced report")
            return _tool_call_response(
                "web_search", '{"query": "x"}', f"c{len(events)}"
            )

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=ToolResult.success(data={"results": []}),
        ), patch(
            "agent_assistant.ui.bridge.api_bridge", FakeBridge()
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        notices = [
            e for e in events
            if e["type"] == "subagent_progress" and e["tool"] == "notice"
        ]
        assert notices and "预算收尾" in notices[0]["label"]
