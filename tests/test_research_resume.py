"""Sub-agent timeout retry, partial-result salvage, and resume-from digest."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_assistant.config import settings
from agent_assistant.tools import research
from agent_assistant.tools.base import ToolResult


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


def _hang_forever(seconds: float = 30.0):
    def _call(**_kwargs):
        time.sleep(seconds)
        return _final_response("too late")

    return _call


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


@pytest.fixture
def fast_timeout(monkeypatch):
    monkeypatch.setattr(research, "_LLM_TURN_TIMEOUT_S", 1)


class TestTimeoutRetry:
    def test_timeout_retries_once_then_succeeds(self, tmp_data_dir, fast_timeout):
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(30)  # first attempt stalls → guarded timeout
            return _final_response("recovered report")

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ):
            result = research.run_research_subagent("test goal", max_turns=5)

        assert result.ok, f"expected success after retry, got {result.error}"
        assert calls["n"] == 2  # one timeout + one successful retry
        assert result.data["summary"] == "recovered report"

    def test_timeout_twice_salvages_partial(self, tmp_data_dir, fast_timeout):
        calls = {"n": 0}

        def chat_side_effect(messages, tools):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call_response("web_search", '{"query": "tesla"}')
            time.sleep(30)  # turn-2 LLM call + its retry both stall

        search_result = ToolResult.success(
            data={"results": [{"title": "Tesla CN", "url": "https://example.com/tesla"}]}
        )

        with patch.object(
            research.llm_client, "chat", side_effect=chat_side_effect
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=search_result,
        ):
            result = research.run_research_subagent("tesla research", max_turns=5)

        assert not result.ok
        assert result.error_category == "subagent_timeout"
        data = result.data
        assert data["resume_from"]
        assert data["findings_count"] >= 1
        assert "example.com/tesla" in data["summary"]
        # Structured findings inlined: claim + sources + l1_refs
        assert data["findings"], "expected inline findings"
        f0 = data["findings"][0]
        assert f0["claim"]
        assert "https://example.com/tesla" in f0["sources"]
        assert f0["l1_refs"]
        # Salvaged artifacts exist on disk for a future resume
        run_dir = tmp_data_dir / "research_runs" / data["resume_from"]
        assert (run_dir / "findings.md").exists()
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "failed"
        assert "timeout" in meta["error"]


class TestResumeDigest:
    def _seed_previous_run(self, data_dir, run_id: str) -> None:
        run_dir = data_dir / "research_runs" / run_id
        run_dir.mkdir(parents=True)
        events = [
            {
                "id": "E1",
                "ts": "2026-08-08T09:00:00+00:00",
                "turn": 1,
                "tool": "web_search",
                "args": {"query": "tesla model y"},
                "ok": True,
                "summary": "Tesla Model Y page",
                "source_refs": ["https://example.com/model-y"],
            },
            {
                "id": "E2",
                "ts": "2026-08-08T09:01:00+00:00",
                "turn": 2,
                "tool": "read_page",
                "args": {"url": "https://example.com/model-y"},
                "ok": True,
                "summary": "Model Y specs: 250 km/h top speed",
                "source_refs": ["https://example.com/model-y"],
            },
        ]
        with (run_dir / "trace.jsonl").open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def test_resume_digest_injected_into_prompt(self, tmp_data_dir):
        self._seed_previous_run(tmp_data_dir, "prevrun12345")
        captured: dict = {}

        def chat_capture(messages, tools):
            captured["system"] = messages[0]["content"]
            return _final_response("resumed report")

        with patch.object(research.llm_client, "chat", side_effect=chat_capture):
            result = research.run_research_subagent(
                "tesla research", max_turns=3, resume_from="prevrun12345"
            )

        assert result.ok
        system = captured["system"]
        assert "RESUME" in system
        assert "https://example.com/model-y" in system
        assert "250 km/h" in system  # claim carried over

    def test_resume_digest_missing_run_starts_fresh(self, tmp_data_dir):
        captured: dict = {}

        def chat_capture(messages, tools):
            captured["system"] = messages[0]["content"]
            return _final_response("fresh report")

        with patch.object(research.llm_client, "chat", side_effect=chat_capture):
            result = research.run_research_subagent(
                "goal", max_turns=3, resume_from="nonexistent99"
            )

        assert result.ok
        assert "starting fresh" in captured["system"]

    def test_dispatch_tool_passes_resume_from(self):
        from agent_assistant.tools.research import DispatchResearchTool

        with patch(
            "agent_assistant.tools.research.run_research_subagent",
            return_value=ToolResult.success(data={"summary": "ok"}),
        ) as mock_run:
            result = DispatchResearchTool().execute(
                goal="g", resume_from="abc123"
            )

        assert result.ok
        assert mock_run.call_args.kwargs["resume_from"] == "abc123"

    def test_dispatch_tool_default_resume_is_none(self):
        from agent_assistant.tools.research import DispatchResearchTool

        with patch(
            "agent_assistant.tools.research.run_research_subagent",
            return_value=ToolResult.success(data={"summary": "ok"}),
        ) as mock_run:
            DispatchResearchTool().execute(goal="g")

        assert mock_run.call_args.kwargs["resume_from"] is None


class TestFailureFeedbackHint:
    def test_subagent_timeout_hint_exists(self):
        from agent_assistant.tools.failure_feedback import build_failure_payload

        payload = build_failure_payload(
            error="sub-agent LLM timed out twice",
            error_category="subagent_timeout",
            data={"resume_from": "xyz"},
        )
        hint = payload["feedback"]["hint"]
        assert "resume_from" in hint
        assert "partial findings" in hint.lower()


class TestReturnShape:
    def test_success_includes_structured_findings(self, tmp_data_dir):
        search_result = ToolResult.success(
            data={"results": [{"title": "T1", "url": "https://example.com/x"}]}
        )
        responses = iter([
            _tool_call_response("web_search", '{"query": "q"}'),
            _final_response("report with sources"),
        ])

        with patch.object(
            research.llm_client, "chat", side_effect=lambda **_: next(responses)
        ), patch(
            "agent_assistant.tools.web_tools.WebSearchTool.execute",
            return_value=search_result,
        ):
            result = research.run_research_subagent("goal", max_turns=5)

        assert result.ok
        data = result.data
        assert data["summary"] == "report with sources"
        assert data["findings_count"] >= 1
        f0 = data["findings"][0]
        assert set(f0.keys()) == {"claim", "sources", "l1_refs"}
        assert "https://example.com/x" in f0["sources"]

    def test_inline_findings_capped(self):
        findings = [
            {"claim": f"c{i}", "sources": [f"https://e.com/{i}"], "l1_refs": [f"E{i}"]}
            for i in range(30)
        ]
        inline = research._inline_findings(findings)
        assert len(inline) == 15
        assert inline[0]["claim"] == "c0"
