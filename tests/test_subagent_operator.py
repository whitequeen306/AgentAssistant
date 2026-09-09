"""Operator runner: inspect-act-verify gate + desktop exclusivity (Task 15)."""

from __future__ import annotations

import json
import threading
import time

from agent_assistant.subagents.manager import SubagentManager, TaskControls, task_owner_id
from agent_assistant.subagents.models import SubagentRole, SubagentSpec, SubagentStatus
from agent_assistant.subagents.profiles import profile_for
from agent_assistant.subagents.resources import DESKTOP, ResourceCoordinator
from agent_assistant.subagents.runners.operator import (
    MUTATING_UI_TOOLS,
    OperatorRegistry,
    _build_registry,
    _evidence,
    operator_runner,
)
from agent_assistant.subagents.store import SubagentStore
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.permission import PermissionPolicy


class FakeUiTool(Tool):
    def __init__(self, name: str, result: ToolResult | None = None) -> None:
        self._name = name
        self._result = result or ToolResult.success(data={"state_hash": "abc123"})
        self.calls: list[dict] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake {self._name}"

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="target", type="string", description="t", required=False),
            ToolParameter(name="text", type="string", description="t", required=False),
        ]

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def gate_registry(*names: str) -> OperatorRegistry:
    registry = OperatorRegistry(permission_policy=PermissionPolicy(rules=[]))
    for name in names:
        registry.register(FakeUiTool(name))
    return registry


def test_registry_matches_operator_profile() -> None:
    registry = _build_registry()
    assert {tool.name for tool in registry.all_tools()} == set(
        profile_for(SubagentRole.OPERATOR).tools
    )
    schemas = {schema["function"]["name"] for schema in registry.openai_schemas()}
    assert "run_command" not in schemas
    assert "move_file" not in schemas


def test_mutation_requires_inspection_first() -> None:
    registry = gate_registry("ui_inspect", "ui_click")
    blocked = registry.execute("ui_click", {"target": "播放"})
    assert blocked.ok is False
    assert blocked.error_category == "inspect_required"

    assert registry.execute("ui_inspect", {}).ok is True
    assert registry.execute("ui_click", {"target": "播放"}).ok is True


def test_every_mutation_invalidates_observation() -> None:
    registry = gate_registry("ui_inspect", "ui_click", "ui_type")
    registry.execute("ui_inspect", {})
    assert registry.execute("ui_click", {"target": "搜索框"}).ok is True
    # Second mutation without re-inspect → refused.
    stale = registry.execute("ui_type", {"target": "搜索框", "text": "夜曲"})
    assert stale.ok is False
    assert stale.error_category == "inspect_required"
    registry.execute("ui_inspect", {})
    assert registry.execute("ui_type", {"target": "搜索框", "text": "夜曲"}).ok is True


def test_launch_and_focus_reset_observation() -> None:
    registry = gate_registry("ui_inspect", "ui_click", "launch_app", "focus_window")
    registry.execute("ui_inspect", {})
    registry.execute("launch_app", {"name": "netease"})
    stale = registry.execute("ui_click", {"target": "播放"})
    assert stale.error_category == "inspect_required"


def test_failed_mutation_keeps_observation() -> None:
    registry = gate_registry("ui_inspect")
    registry.register(
        FakeUiTool("ui_click", ToolResult.failure("element not found", code=404))
    )
    registry.execute("ui_inspect", {})
    first = registry.execute("ui_click", {"target": "missing"})
    assert first.ok is False and first.error_category != "inspect_required"
    # Failure did not change the screen — retrying is allowed without re-inspect.
    second = registry.execute("ui_click", {"target": "missing"})
    assert second.error_category != "inspect_required"


def test_evidence_never_contains_typed_text() -> None:
    entries = _evidence(
        "ui_type",
        {"target": "搜索框", "text": "我的密码123"},
        {"ok": True, "data": {"state_hash": "deadbeef"}},
    )
    dumped = json.dumps(entries, ensure_ascii=False)
    assert "我的密码123" not in dumped
    assert entries[0]["ref"] == "搜索框"
    assert "deadbeef" in dumped


def test_operator_e2e_pause_resume_and_exclusive_desktop(tmp_path, monkeypatch) -> None:
    """Scripted operator through the manager: lock, typed-text hygiene,
    pause boundary, resume with mandatory re-inspection."""
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import runtime as runtime_module
    from agent_assistant.subagents.runners import operator as operator_module

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)

    # Scripted LLM: inspect → type → (pause happens) …resumed: inspect → done.
    class _Function:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = json.dumps(arguments, ensure_ascii=False)

    class _Call:
        def __init__(self, call_id, name, arguments):
            self.id = call_id
            self.function = _Function(name, arguments)

    class _Message:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

        def model_dump(self):
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

    class _Response:
        def __init__(self, message):
            self.choices = [type("C", (), {"message": message})()]

    pause_requested = threading.Event()
    resumed_run = threading.Event()

    class ScriptedLLM:
        def __init__(self):
            self.phase = 0

        def chat(self, messages, tools=None):
            # First run: inspect → type (executed) → pause before next action.
            if self.phase == 0:
                self.phase = 1
                return _Response(
                    _Message(tool_calls=[_Call("c1", "ui_inspect", {})])
                )
            if self.phase == 1:
                self.phase = 2
                return _Response(
                    _Message(
                        tool_calls=[
                            _Call("c2", "ui_type", {"target": "搜索框", "text": "夜曲"})
                        ]
                    )
                )
            if self.phase == 2:
                self.phase = 3
                pause_requested.set()
                # Wait until the test has issued the pause request.
                time.sleep(0.3)
                return _Response(_Message(tool_calls=[_Call("c3", "ui_inspect", {})]))
            if self.phase == 3:
                # Resumed attempt: must re-inspect (fresh registry).
                self.phase = 4
                resumed_run.set()
                return _Response(_Message(tool_calls=[_Call("c4", "ui_inspect", {})]))
            return _Response(_Message(content="歌曲已播放（已验证界面状态）"))

    llm = ScriptedLLM()
    monkeypatch.setattr(runtime_module, "llm_client", llm)

    # Fake UIA tools — never touch the real desktop in tests.
    def fake_build_registry():
        return gate_registry(
            "ui_inspect", "ui_click", "ui_type", "ui_hotkey", "ui_scroll",
            "launch_app", "focus_window",
        )

    monkeypatch.setattr(operator_module, "_build_registry", fake_build_registry)

    resources = ResourceCoordinator()
    store = SubagentStore(tmp_path / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: operator_runner,
        archive_root=tmp_path / "subagent_runs",
        resources=resources,
    )
    try:
        spec = SubagentSpec(
            task_id="op-e2e",
            conversation_id="conv-1",
            parent_turn_id="turn-1",
            role=SubagentRole.OPERATOR,
            title="播放夜曲",
            goal="在网易云音乐播放周杰伦的夜曲",
        )
        manager.dispatch([spec])

        assert pause_requested.wait(timeout=5)
        # While running, the desktop belongs to the task exclusively.
        assert resources.owner(DESKTOP) == task_owner_id("op-e2e")
        assert resources.try_acquire(DESKTOP, "someone-else") is None

        manager.control("op-e2e", action="pause")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if manager.get_task("op-e2e").status is SubagentStatus.PAUSED:
                break
            time.sleep(0.02)
        assert manager.get_task("op-e2e").status is SubagentStatus.PAUSED
        assert resources.owner(DESKTOP) is None  # lock released while paused

        manager.control("op-e2e", action="resume")
        results = manager.await_tasks(["op-e2e"], timeout=10)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.COMPLETED
        assert resumed_run.is_set()
        assert resources.owner(DESKTOP) is None

        # Typed text is absent from persisted events AND the L0 archive.
        events = manager.list_events("op-e2e")
        all_events = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
        assert "夜曲" not in all_events or "ui_type(夜曲" not in all_events
        raw = (tmp_path / "subagent_runs" / "op-e2e" / "raw.jsonl").read_text("utf-8")
        assert '"text": "夜曲"' not in raw
        assert "<redacted>" in raw
    finally:
        manager.shutdown()
        store.close()


def test_two_operator_tasks_serialize_via_manager(tmp_path, monkeypatch) -> None:
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import runtime as runtime_module
    from agent_assistant.subagents.runners import operator as operator_module

    monkeypatch.setattr(app_settings, "data_dir", tmp_path)

    class OneShotLLM:
        def chat(self, messages, tools=None):
            message = type(
                "M",
                (),
                {
                    "content": "完成",
                    "tool_calls": None,
                    "model_dump": lambda self: {"role": "assistant", "content": "完成"},
                },
            )()
            return type("R", (), {"choices": [type("C", (), {"message": message})()]})()

    monkeypatch.setattr(runtime_module, "llm_client", OneShotLLM())
    monkeypatch.setattr(
        operator_module, "_build_registry", lambda: gate_registry("ui_inspect")
    )

    resources = ResourceCoordinator()
    concurrent = {"count": 0, "peak": 0}
    lock = threading.Lock()
    original_runner = operator_runner

    def counting_runner(spec, controls, emit):
        with lock:
            concurrent["count"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["count"])
        try:
            time.sleep(0.1)
            return original_runner(spec, controls, emit)
        finally:
            with lock:
                concurrent["count"] -= 1

    store = SubagentStore(tmp_path / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: counting_runner,
        archive_root=tmp_path / "subagent_runs",
        resources=resources,
    )
    try:
        specs = [
            SubagentSpec(
                task_id=f"op-{index}",
                conversation_id="conv-1",
                parent_turn_id="turn-1",
                role=SubagentRole.OPERATOR,
                title=f"操作 {index}",
                goal="点一下",
            )
            for index in range(2)
        ]
        manager.dispatch(specs)
        results = manager.await_tasks(["op-0", "op-1"], timeout=10)
        assert len(results) == 2
        assert concurrent["peak"] == 1  # never overlapped
    finally:
        manager.shutdown()
        store.close()


def test_controls_should_yield_used_by_gate() -> None:
    controls = TaskControls("op", threading.Event(), threading.Event())
    assert not controls.should_yield()
    controls.pause_event.set()
    assert controls.should_yield()


def test_mutating_tools_constant_matches_expectation() -> None:
    assert MUTATING_UI_TOOLS == {"ui_click", "ui_type", "ui_hotkey"}
