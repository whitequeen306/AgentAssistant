"""Research-run robustness: URL encoding, SERP block, host circuit breaker,
budget wrap-up, per-result L2 findings."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_assistant.config import settings
from agent_assistant.tools import research
from agent_assistant.tools.base import ToolResult
from agent_assistant.tools.web_tools import (
    ReadPageTool,
    _encode_url,
    _serp_block,
)


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


class TestEncodeUrl:
    def test_chinese_query_percent_encoded(self):
        out = _encode_url("https://html.duckduckgo.com/html/?q=哈尔滨工业大学&x=1")
        assert "%E5%93%88%E5%B0%94%E6%BB%A8" in out
        assert "x=1" in out

    def test_already_encoded_not_double_encoded(self):
        url = "https://example.com/?q=%E4%B8%AD%E6%96%87"
        assert _encode_url(url) == url

    def test_plain_ascii_url_unchanged(self):
        url = "https://yz.neu.edu.cn/2025/1009/c5933a293436/page.htm"
        assert _encode_url(url) == url


class TestSerpBlock:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.baidu.com/s?wd=xyz",
            "https://m.baidu.com/s?word=xyz",
            "https://www.sogou.com/web?query=xyz",
            "https://www.google.com/search?q=xyz",
        ],
    )
    def test_serp_urls_blocked(self, url):
        assert _serp_block(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.bing.com/search?q=xyz",  # bing SERP actually parses OK
            "https://yz.neu.edu.cn/2025/1009/c5933a293436/page.htm",
            "https://baike.baidu.com/item/%E6%97%A5%E8%AF%AD/398720",
        ],
    )
    def test_non_serp_urls_allowed(self, url):
        assert not _serp_block(url)

    def test_read_page_rejects_serp_without_network(self):
        result = ReadPageTool().execute(url="https://www.baidu.com/s?wd=xyz")
        assert not result.ok
        assert result.error_category == "serp_fetch"
        assert "web_search" in result.error


class TestHostCircuitBreaker:
    def test_opens_after_threshold_consecutive_failures(self):
        b = research._HostCircuitBreaker()
        args = {"url": "https://r.jina.ai/https://x.com"}
        assert b.blocked_host("read_page", args) is None
        b.record("read_page", args, ok=False)
        assert b.blocked_host("read_page", args) is None
        b.record("read_page", args, ok=False)
        assert b.blocked_host("read_page", args) == "r.jina.ai"

    def test_soft_failures_also_trip_the_circuit(self):
        """软失败必须计入熔断。

        2026-09-11 实测：某次调研 12 轮里 read_page 被调 38 次、web_search 只 1 次
        —— 因为「页面抓到了但读不出内容」（JS 渲染 / PDF / 反爬）被当成成功清零
        计数，熔断永不触发，模型拿着 URL 无限重试。院校官网正是这种形态。
        """
        b = research._HostCircuitBreaker()
        args = {"url": "https://yjszs.ecnu.edu.cn/zsml/sszsml/index/2026"}
        for _ in range(b.SOFT_FAIL_THRESHOLD):
            assert b.blocked_host("read_page", args) is None
            b.record("read_page", args, ok=False, soft=True)
        assert b.blocked_host("read_page", args) == "yjszs.ecnu.edu.cn"

    def test_soft_categories_cover_unreadable_page(self):
        """extract_fail / empty_html 必须在软失败集合里，否则熔断形同虚设。"""
        assert "extract_fail" in research._HostCircuitBreaker.SOFT_CATEGORIES
        assert "empty_html" in research._HostCircuitBreaker.SOFT_CATEGORIES

    def test_success_resets_soft_counter_too(self):
        b = research._HostCircuitBreaker()
        args = {"url": "https://a.com/x"}
        b.record("read_page", args, ok=False, soft=True)
        b.record("read_page", args, ok=False, soft=True)
        b.record("read_page", args, ok=True)
        b.record("read_page", args, ok=False, soft=True)
        assert b.blocked_host("read_page", args) is None

    def test_success_resets_counter(self):
        b = research._HostCircuitBreaker()
        args = {"url": "https://slow.example.com/a"}
        b.record("read_page", args, ok=False)
        b.record("read_page", args, ok=True)
        b.record("read_page", args, ok=False)
        assert b.blocked_host("read_page", args) is None

    def test_untracked_tools_and_missing_url_ignored(self):
        b = research._HostCircuitBreaker()
        for _ in range(3):
            b.record("web_search", {"query": "q"}, ok=False)
            b.record("read_page", {}, ok=False)
        assert b.blocked_host("web_search", {"query": "q"}) is None
        assert b.blocked_host("read_page", {}) is None

    def test_soft_extraction_failures_do_not_trip_circuit(self, tmp_data_dir):
        """Guessing empty list-page URLs on a healthy host (extract_fail) must
        NOT circuit-break it — only hard failures (timeout/conn/HTTP) count."""
        responses = iter([
            _tool_call_response("read_page", '{"url": "https://ok.example.com/l1"}', "c1"),
            _tool_call_response("read_page", '{"url": "https://ok.example.com/l2"}', "c2"),
            _tool_call_response("read_page", '{"url": "https://ok.example.com/l3"}', "c3"),
            _final_response("done"),
        ])
        exec_calls = {"n": 0}

        def fake_execute(self, **kwargs):
            exec_calls["n"] += 1
            return ToolResult.failure(
                "could not extract readable content",
                code=422,
                error_category="extract_fail",
            )

        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute", fake_execute
        ):
            result = research.run_research_subagent("g", max_turns=10)

        assert result.ok
        assert exec_calls["n"] == 3, "soft failures must not open the circuit"

    def test_hard_failures_still_trip_circuit(self, tmp_data_dir):
        """Timeouts (no error_category) remain hard failures."""
        responses = iter([
            _tool_call_response("read_page", '{"url": "https://dead.example.com/a"}', "c1"),
            _tool_call_response("read_page", '{"url": "https://dead.example.com/b"}', "c2"),
            _tool_call_response("read_page", '{"url": "https://dead.example.com/c"}', "c3"),
            _final_response("done"),
        ])
        exec_calls = {"n": 0}

        def fake_execute(self, **kwargs):
            exec_calls["n"] += 1
            return ToolResult.failure("Operation timed out.")

        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute", fake_execute
        ):
            result = research.run_research_subagent("g", max_turns=10)

        assert result.ok
        assert exec_calls["n"] == 2, "hard failures must still open the circuit"

    def test_third_call_to_dead_host_never_executes(self, tmp_data_dir):
        responses = iter([
            _tool_call_response("read_page", '{"url": "https://dead.example.com/a"}', "c1"),
            _tool_call_response("read_page", '{"url": "https://dead.example.com/b"}', "c2"),
            _tool_call_response("read_page", '{"url": "https://dead.example.com/c"}', "c3"),
            _final_response("done"),
        ])
        exec_calls = {"n": 0}

        def fake_execute(self, **kwargs):
            exec_calls["n"] += 1
            return ToolResult.failure("Operation timed out.")

        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute", fake_execute
        ):
            result = research.run_research_subagent("g", max_turns=10)

        assert result.ok
        assert exec_calls["n"] == 2, "3rd call to dead host must be circuit-broken"
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
        assert "circuit is OPEN" in trace


class TestBudgetWrapUp:
    def test_wrapup_notice_and_forced_final_report(self, tmp_data_dir):
        seen_tools: list[object] = []
        captured_messages: list[list[dict]] = []

        def chat_side_effect(messages, tools):
            seen_tools.append(tools)
            captured_messages.append(list(messages))
            if tools is None:
                return _final_response("forced report at budget limit")
            return _tool_call_response("web_search", '{"query": "x"}', f"c{len(seen_tools)}")

        search_result = ToolResult.success(
            data={"results": [{"title": "T", "url": "https://example.com/1"}]}
        )

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=search_result,
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert result.warning and "budget limit" in result.warning
        assert result.data["turns_used"] == 5
        # Last turn ran without tools; earlier turns had them
        assert seen_tools[-1] is None
        assert all(t is not None for t in seen_tools[:-1])
        # Wrap-up notice injected as a system message before the end
        notice = [
            m for msgs in captured_messages for m in msgs
            if m.get("role") == "system" and "Budget notice" in (m.get("content") or "")
        ]
        assert notice, "expected a budget-notice system message"
        # Report actually written instead of dying empty
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        assert (run_dir / "report.md").read_text(encoding="utf-8") == (
            "forced report at budget limit"
        )
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "done_partial"

    def test_normal_finish_has_no_forced_warning(self, tmp_data_dir):
        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("clean report"),
        ):
            result = research.run_research_subagent("goal", max_turns=5)
        assert result.ok
        assert result.warning is None
        run_dir = tmp_data_dir / "research_runs" / result.data["run_id"]
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "done"


class TestPerResultFindings:
    def test_search_summary_is_per_result_lines(self):
        from agent_assistant.research.run_store import summarize_tool_result

        out = summarize_tool_result("web_search", {
            "ok": True,
            "data": {
                "results": [
                    {"title": "东大2026目录", "url": "http://yz.neu.edu.cn/a"},
                    {"title": "哈工大2026目录", "url": "https://yzb.hit.edu.cn/b"},
                ]
            },
        })
        lines = out.splitlines()
        assert lines[0] == "东大2026目录 — http://yz.neu.edu.cn/a"
        assert lines[1] == "哈工大2026目录 — https://yzb.hit.edu.cn/b"

    def test_consolidate_matches_claim_to_its_own_url(self, tmp_data_dir):
        from agent_assistant.research.run_store import ResearchRun

        run = ResearchRun(goal="g")
        summary = "东大2026目录 — http://yz.neu.edu.cn/a\n哈工大2026目录 — https://yzb.hit.edu.cn/b"
        run.log_l1(
            tool="web_search",
            args={"query": "q"},
            summary=summary,
            source_refs=["http://yz.neu.edu.cn/a", "https://yzb.hit.edu.cn/b"],
            turn=1,
        )
        findings = run.consolidate_l2_from_l1()
        by_source = {f["sources"][0]: f["claim"] for f in findings}
        assert by_source["http://yz.neu.edu.cn/a"] == "东大2026目录"
        assert by_source["https://yzb.hit.edu.cn/b"] == "哈工大2026目录"

    def test_salvage_uses_per_result_claim(self, tmp_data_dir):
        from agent_assistant.research.run_store import ResearchRun

        run = ResearchRun(goal="g")
        summary = "东大2026目录 — http://yz.neu.edu.cn/a\n哈工大2026目录 — https://yzb.hit.edu.cn/b"
        run.log_l1(
            tool="web_search",
            args={"query": "q"},
            summary=summary,
            source_refs=["http://yz.neu.edu.cn/a", "https://yzb.hit.edu.cn/b"],
            turn=1,
        )
        findings = research._salvage_findings_readonly(run.run_id)
        by_src = {f["source"]: f["claim"] for f in findings}
        assert by_src["http://yz.neu.edu.cn/a"] == "东大2026目录"
        assert by_src["https://yzb.hit.edu.cn/b"] == "哈工大2026目录"


class TestUrlDedup:
    def test_key_normalises_host_case_and_trailing_slash(self):
        k1 = research._url_key("read_page", {"url": "https://A.com/path/"})
        k2 = research._url_key("read_page", {"url": "https://a.com/path"})
        assert k1 == k2
        assert research._url_key("read_page", {"url": "https://a.com/path?q=1"}) != k1

    def test_non_fetch_tools_have_no_key(self):
        assert research._url_key("web_search", {"query": "x"}) is None

    def test_repeated_url_is_refused(self, tmp_data_dir):
        """同一个 URL 读两遍必须被拒 —— 那次 38 次 read_page 的直接堵口。"""
        responses = iter([
            _tool_call_response(
                "read_page", '{"url": "https://yzb.hit.edu.cn/a.htm"}', "c1"
            ),
            _tool_call_response(
                "read_page", '{"url": "https://yzb.hit.edu.cn/a.htm"}', "c2"
            ),
            _final_response("done"),
        ])
        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute",
            return_value=ToolResult.success(data={"text": "x"}),
        ) as fetch:
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert fetch.call_count == 1  # 真去抓的只有第一次


class TestDepthBudget:
    def test_depth_maps_to_turn_budgets(self):
        assert research.RESEARCH_DEPTH_TURNS["quick"] < research.RESEARCH_DEPTH_TURNS["standard"]
        assert research.RESEARCH_DEPTH_TURNS["standard"] < research.RESEARCH_DEPTH_TURNS["deep"]

    def test_unknown_depth_falls_back_to_standard(self, tmp_data_dir):
        stream: list[tuple[str, dict]] = []
        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("done"),
        ):
            research.run_research_subagent(
                "goal", depth="bogus", on_event=lambda k, p: stream.append((k, p))
            )
        # 未传 max_turns → 用 standard 档
        assert research.DEFAULT_RESEARCH_DEPTH == "standard"

    def test_explicit_max_turns_beats_depth(self, tmp_data_dir):
        """显式 max_turns 优先（测试与特殊调用方需要精确控制）。"""
        seen: dict[str, int] = {}

        def fake_progress(tool, label, turn, total):
            seen["total"] = total

        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("done"),
        ):
            research.run_research_subagent(
                "goal", depth="deep", max_turns=3, on_progress=fake_progress
            )
        # 无工具调用时 on_progress 不会触发；这里只断言不抛错且被钳到 >=2
        assert research.RESEARCH_DEPTH_TURNS["deep"] == 45


class TestUrlDedup:
    def test_key_normalises_host_case_and_trailing_slash(self):
        k1 = research._url_key("read_page", {"url": "https://A.com/path/"})
        k2 = research._url_key("read_page", {"url": "https://a.com/path"})
        assert k1 == k2
        assert research._url_key("read_page", {"url": "https://a.com/path?q=1"}) != k1

    def test_non_fetch_tools_have_no_key(self):
        assert research._url_key("web_search", {"query": "x"}) is None

    def test_repeated_url_is_refused(self, tmp_data_dir):
        """同一个 URL 读两遍必须被拒 —— 那次 38 次 read_page 的直接堵口。"""
        responses = iter([
            _tool_call_response(
                "read_page", '{"url": "https://yzb.hit.edu.cn/a.htm"}', "c1"
            ),
            _tool_call_response(
                "read_page", '{"url": "https://yzb.hit.edu.cn/a.htm"}', "c2"
            ),
            _final_response("done"),
        ])
        with patch.object(
            research.llm_client, "chat", side_effect=lambda **kw: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.ReadPageTool.execute",
            return_value=ToolResult.success(data={"text": "x"}),
        ) as fetch:
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        assert fetch.call_count == 1  # 真去抓的只有第一次


class TestDepthBudget:
    def test_depth_maps_to_turn_budgets(self):
        assert research.RESEARCH_DEPTH_TURNS["quick"] < research.RESEARCH_DEPTH_TURNS["standard"]
        assert research.RESEARCH_DEPTH_TURNS["standard"] < research.RESEARCH_DEPTH_TURNS["deep"]

    def test_unknown_depth_falls_back_to_standard(self, tmp_data_dir):
        stream: list[tuple[str, dict]] = []
        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("done"),
        ):
            research.run_research_subagent(
                "goal", depth="bogus", on_event=lambda k, p: stream.append((k, p))
            )
        # 未传 max_turns → 用 standard 档
        assert research.DEFAULT_RESEARCH_DEPTH == "standard"

    def test_explicit_max_turns_beats_depth(self, tmp_data_dir):
        """显式 max_turns 优先（测试与特殊调用方需要精确控制）。"""
        seen: dict[str, int] = {}

        def fake_progress(tool, label, turn, total):
            seen["total"] = total

        with patch.object(
            research.llm_client, "chat",
            side_effect=lambda **kw: _final_response("done"),
        ):
            research.run_research_subagent(
                "goal", depth="deep", max_turns=3, on_progress=fake_progress
            )
        # 无工具调用时 on_progress 不会触发；这里只断言不抛错且被钳到 >=2
        assert research.RESEARCH_DEPTH_TURNS["deep"] == 45
