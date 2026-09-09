"""Generic runtime + Explorer runner (Task 14)."""

from __future__ import annotations

import json
import threading

from agent_assistant.subagents.archive import TaskArchive
from agent_assistant.subagents.manager import PausedOutcome, TaskControls
from agent_assistant.subagents.models import SubagentRole, SubagentSpec, SubagentStatus
from agent_assistant.subagents.profiles import profile_for
from agent_assistant.subagents.runtime import run_task_loop
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.permission import PermissionPolicy
from agent_assistant.tools.registry import ToolRegistry

# ─── Fakes ───────────────────────────────────────────────────────────────────


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.function = _FakeFunction(name, json.dumps(arguments, ensure_ascii=False))


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self) -> dict:
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in (self.tool_calls or [])
            ]
            or None,
        }


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [type("Choice", (), {"message": message})()]


class ScriptedLLM:
    """Returns scripted turns; records every request for assertions."""

    def __init__(self, turns) -> None:
        self._turns = list(turns)
        self.requests: list[dict] = []

    def chat(self, messages, tools=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self._turns:
            return _FakeResponse(_FakeMessage(content="（无更多脚本）"))
        return _FakeResponse(self._turns.pop(0))


class RecorderTool(Tool):
    def __init__(self, name: str, result: ToolResult) -> None:
        self._name = name
        self._result = result
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake {self._name}"

    @property
    def parameters(self) -> list[ToolParameter]:
        return [ToolParameter(name="path", type="string", description="p", required=False)]

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def make_spec(**overrides) -> SubagentSpec:
    values = dict(
        task_id="explore1",
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=SubagentRole.EXPLORER,
        title="找考研资料",
        goal="在 Documents 里找考研资料",
    )
    values.update(overrides)
    return SubagentSpec(**values)


def make_controls() -> TaskControls:
    return TaskControls("explore1", threading.Event(), threading.Event())


def allow_all_registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry(permission_policy=PermissionPolicy(rules=[]))
    for tool in tools:
        registry.register(tool)
    return registry


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_scripted_search_read_answer_flow(tmp_path) -> None:
    search = RecorderTool(
        "search_local_files",
        ToolResult.success(
            data={"results": [{"path": "C:/docs/考研.pdf", "match": "name"}], "count": 1}
        ),
    )
    read = RecorderTool("read_file", ToolResult.success(data={"content": "考研大纲内容"}))
    llm = ScriptedLLM(
        [
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall("c1", "search_local_files", {"root": "C:/docs", "query": "考研"})
                ]
            ),
            _FakeMessage(
                tool_calls=[_FakeToolCall("c2", "read_file", {"path": "C:/docs/考研.pdf"})]
            ),
            _FakeMessage(content="# 找到了\n\nC:/docs/考研.pdf 已核实。"),
        ]
    )
    events: list[dict] = []
    archive = TaskArchive(tmp_path, "explore1")

    def extractor(tool, args, result_dict):
        if tool == "read_file":
            return [{"type": "file_read", "ref": args.get("path", ""), "claim": "已读取"}]
        return []

    result = run_task_loop(
        make_spec(),
        make_controls(),
        lambda event_type, payload: events.append(payload),
        registry=allow_all_registry(search, read),
        system_prompt="You are the Explorer.",
        max_turns=10,
        archive=archive,
        llm=llm,
        evidence_extractor=extractor,
    )

    assert result.status is SubagentStatus.COMPLETED
    assert "找到了" in result.summary
    assert result.output.startswith("# 找到了")
    assert result.evidence == [
        {"type": "file_read", "ref": "C:/docs/考研.pdf", "claim": "已读取"}
    ]
    assert result.stats["turns_used"] == 3
    assert search.calls and read.calls
    # Events carry per-tool labels with turn numbers.
    assert any("search_local_files" in event.get("label", "") for event in events)
    # L0 archived both tool calls; output.md written.
    raw_lines = archive.raw_path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert archive.output_path.read_text(encoding="utf-8").startswith("# 找到了")


def test_only_allowlisted_tools_are_exposed_to_llm(tmp_path) -> None:
    from agent_assistant.subagents.runners.explorer import _build_registry

    registry = _build_registry()
    names = {tool.name for tool in registry.all_tools()}
    assert names == set(profile_for(SubagentRole.EXPLORER).tools)
    schemas = registry.openai_schemas()
    schema_names = {schema["function"]["name"] for schema in schemas}
    assert "move_file" not in schema_names
    assert "run_command" not in schema_names
    assert "ui_click" not in schema_names


def test_old_tool_results_are_stubbed_but_archived(tmp_path) -> None:
    tool = RecorderTool("read_file", ToolResult.success(data={"content": "内容" * 50}))
    turns = [
        _FakeMessage(tool_calls=[_FakeToolCall(f"c{i}", "read_file", {"path": f"f{i}"})])
        for i in range(10)
    ] + [_FakeMessage(content="完成")]
    llm = ScriptedLLM(turns)
    archive = TaskArchive(tmp_path, "explore1")

    result = run_task_loop(
        make_spec(),
        make_controls(),
        lambda *_: None,
        registry=allow_all_registry(tool),
        system_prompt="Explorer.",
        max_turns=20,
        archive=archive,
        llm=llm,
    )
    assert result.status is SubagentStatus.COMPLETED
    # The final API request must contain stubbed old tool messages.
    final_request = llm.requests[-1]["messages"]
    tool_messages = [m for m in final_request if m.get("role") == "tool"]
    stubbed = [m for m in tool_messages if '"stubbed": true' in m.get("content", "")]
    full = [m for m in tool_messages if '"stubbed": true' not in m.get("content", "")]
    assert len(tool_messages) == 10
    assert len(full) == 8  # recent window
    assert len(stubbed) == 2
    # No internal bookkeeping keys leak into API requests.
    assert all("_stubbed" not in m for m in final_request)
    # L0 keeps every raw record.
    assert len(archive.raw_path.read_text(encoding="utf-8").splitlines()) == 10


def test_cancellation_returns_partial_result(tmp_path) -> None:
    controls = make_controls()
    tool = RecorderTool("read_file", ToolResult.success(data={"content": "x"}))

    def cancel_after_first(tool_name, args, result_dict):
        controls.cancel_event.set()
        return []

    llm = ScriptedLLM(
        [
            _FakeMessage(tool_calls=[_FakeToolCall("c1", "read_file", {"path": "a"})]),
            _FakeMessage(content="不应到达"),
        ]
    )
    result = run_task_loop(
        make_spec(),
        controls,
        lambda *_: None,
        registry=allow_all_registry(tool),
        system_prompt="Explorer.",
        max_turns=10,
        llm=llm,
        evidence_extractor=cancel_after_first,
    )
    assert result.status is SubagentStatus.CANCELLED
    assert result.partial_result["turns_used"] >= 1
    assert result.partial_result["recent_activity"]


def test_pause_yields_paused_outcome_with_checkpoint() -> None:
    controls = make_controls()
    controls.pause_event.set()
    llm = ScriptedLLM([_FakeMessage(content="不应调用")])
    outcome = run_task_loop(
        make_spec(),
        controls,
        lambda *_: None,
        registry=allow_all_registry(),
        system_prompt="Explorer.",
        max_turns=10,
        llm=llm,
    )
    assert isinstance(outcome, PausedOutcome)
    assert "turns_used" in outcome.checkpoint
    assert llm.requests == []  # yielded before any LLM call


def test_wrap_up_turn_forces_final_answer_without_tools() -> None:
    """On the last turn tools are withheld so an answer always exists."""
    tool = RecorderTool("read_file", ToolResult.success(data={"content": "x"}))
    llm = ScriptedLLM(
        [
            _FakeMessage(tool_calls=[_FakeToolCall("c1", "read_file", {"path": "a"})]),
            _FakeMessage(content="最终结论"),
        ]
    )
    result = run_task_loop(
        make_spec(),
        make_controls(),
        lambda *_: None,
        registry=allow_all_registry(tool),
        system_prompt="Explorer.",
        max_turns=2,
        llm=llm,
    )
    assert result.status is SubagentStatus.COMPLETED
    # Second request (last turn) must have tools withheld.
    assert llm.requests[-1]["tools"] is None
    # And the budget notice was injected.
    assert any(
        "[Budget notice]" in m.get("content", "")
        for m in llm.requests[-1]["messages"]
        if m.get("role") == "system"
    )


def test_llm_failure_returns_failure_with_partial() -> None:
    class FailingLLM:
        def chat(self, messages, tools=None):
            raise RuntimeError("boom")

    result = run_task_loop(
        make_spec(),
        make_controls(),
        lambda *_: None,
        registry=allow_all_registry(),
        system_prompt="Explorer.",
        max_turns=3,
        llm=FailingLLM(),
    )
    assert result.status is SubagentStatus.FAILED
    assert result.error_category == "llm_error"
    assert result.partial_result is not None


def test_explorer_runner_via_manager_binds_owner_context(tmp_path, monkeypatch) -> None:
    """End-to-end through the manager with a scripted LLM."""
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import runtime as runtime_module
    from agent_assistant.subagents.manager import SubagentManager
    from agent_assistant.subagents.resources import ResourceCoordinator
    from agent_assistant.subagents.runners.explorer import explorer_runner
    from agent_assistant.subagents.store import SubagentStore

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    llm = ScriptedLLM([_FakeMessage(content="# 探索完成\n\n没有需要读取的文件。")])
    monkeypatch.setattr(runtime_module, "llm_client", llm)

    store = SubagentStore(tmp_path / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: explorer_runner,
        archive_root=tmp_path / "subagent_runs",
        resources=ResourceCoordinator(),
    )
    try:
        manager.dispatch([make_spec(task_id="explorer-e2e")])
        results = manager.await_tasks(["explorer-e2e"], timeout=10)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.COMPLETED
        assert "探索完成" in results[0].summary
        assert results[0].l1_trace  # artifacts recorded
    finally:
        manager.shutdown()
        store.close()
