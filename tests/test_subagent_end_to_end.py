"""Four-role end-to-end scenario (Task 20).

User ask: 帮我找电脑里的考研资料，查今年政策，整理到一个文件夹，
同时在网易云播放周杰伦的《夜曲》。

Explorer + Researcher + Operator use controlled fake runners (overlap, desktop
ownership, injected failure with salvage); Organizer runs its REAL runner with
a scripted LLM (plan → waiting_user → approval → deterministic apply).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agent_assistant.subagents.context import AgentExecutionContext, bind_execution_context
from agent_assistant.subagents.manager import SubagentManager, task_owner_id
from agent_assistant.subagents.models import (
    SubagentResult,
    SubagentRole,
    SubagentStatus,
)
from agent_assistant.subagents.resources import DESKTOP, ResourceCoordinator
from agent_assistant.subagents.runners.organizer import organizer_runner
from agent_assistant.subagents.store import SubagentStore
from agent_assistant.tools.subagent_tools import (
    AwaitSubagentsTool,
    DispatchSubagentsTool,
)


@pytest.fixture()
def jail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_assistant.tools.permission.jail_roots", lambda: [str(tmp_path)]
    )
    return tmp_path


def test_four_role_personal_assistant_scenario(jail, tmp_path, monkeypatch) -> None:
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import runtime as runtime_module

    monkeypatch.setattr(app_settings, "data_dir", tmp_path / "data")

    # Local files the Explorer "finds" and the Organizer moves.
    source = jail / "Documents" / "考研英语真题.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    destination = jail / "Documents" / "考研" / "考研英语真题.pdf"

    resources = ResourceCoordinator()
    # Explorer and the Organizer PLANNING phase must overlap → barrier.
    # 30s: under a loaded machine (AV scanning a fresh PyInstaller dist during
    # the same full-suite run) 10s was observed to flake.
    overlap_barrier = threading.Barrier(2, timeout=30)
    barrier_broken = threading.Event()

    def wait_overlap() -> None:
        try:
            overlap_barrier.wait()
        except threading.BrokenBarrierError:
            barrier_broken.set()

    def explorer_fake(spec, controls, emit):
        emit("activity", {"label": "扫描 Documents"})
        wait_overlap()  # held open until the organizer is also running
        return SubagentResult.completed(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="找到 1 份考研资料",
            output=f"找到 {source}（已读取核实）",
            evidence=[{"type": "file_read", "ref": str(source), "claim": "已核实"}],
        )

    def researcher_fake(spec, controls, emit):
        emit("progress", {"label": "搜索 2026 考研政策", "turn": 1, "max_turns": 60})
        # Injected failure — progress must survive as partial_result.
        return SubagentResult.failure(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="调研中断，部分进度已保留",
            error_category="subagent_timeout",
            error="LLM timed out twice",
            partial_result={
                "summary": "- 已确认初试时间提前",
                "findings": [{"claim": "初试提前一周", "sources": ["https://example.com"]}],
                "resume_from": "run-e2e",
            },
            next_action="retry this task (progress is preserved via research run run-e2e)",
        )

    desktop_owner_seen: dict[str, str | None] = {"owner": None}

    def operator_fake(spec, controls, emit):
        desktop_owner_seen["owner"] = resources.owner(DESKTOP)
        emit("activity", {"label": "在网易云搜索 夜曲"})
        return SubagentResult.completed(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="夜曲已开始播放",
            output="已在网易云音乐播放 周杰伦 - 夜曲（界面已验证）",
        )

    # Organizer uses its REAL runner + scripted planning LLM.
    class OrganizerPlanLLM:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, tools=None):
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

            self.turn += 1
            if self.turn == 1:
                wait_overlap()  # organizer planning overlaps explorer
                return _Response(
                    _Message(
                        tool_calls=[
                            _Call(
                                "c1",
                                "propose_moves",
                                {"moves": [{"src": str(source), "dst": str(destination)}]},
                            )
                        ]
                    )
                )
            return _Response(_Message(content="计划：把考研资料集中到 考研 文件夹。"))

    monkeypatch.setattr(runtime_module, "llm_client", OrganizerPlanLLM())

    runners = {
        SubagentRole.EXPLORER: explorer_fake,
        SubagentRole.RESEARCHER: researcher_fake,
        SubagentRole.OPERATOR: operator_fake,
        SubagentRole.ORGANIZER: organizer_runner,
    }

    store = SubagentStore(tmp_path / "data" / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: runners[role],
        archive_root=tmp_path / "data" / "subagent_runs",
        resources=resources,
    )

    # Approver: as soon as the organizer parks in waiting_user, verify nothing
    # moved and approve with the manifest hash from the persisted event.
    approval_done = threading.Event()
    approval_error: list[str] = []

    def approver() -> None:
        deadline = time.monotonic() + 15
        organizer_id = None
        try:
            while time.monotonic() < deadline:
                for task in manager.list_tasks(conversation_id="conv-e2e"):
                    if task.role is SubagentRole.ORGANIZER:
                        organizer_id = task.task_id
                        if task.status is SubagentStatus.WAITING_USER:
                            if destination.exists() or not source.exists():
                                approval_error.append("moved before approval")
                                return
                            events = manager.list_events(organizer_id)
                            digest = next(
                                event.payload["manifest_hash"]
                                for event in reversed(events)
                                if event.payload.get("manifest_hash")
                            )
                            response = manager.respond_organizer_plan(
                                organizer_id, digest, True
                            )
                            if not response.get("ok"):
                                approval_error.append(str(response))
                            approval_done.set()
                            return
                time.sleep(0.03)
            approval_error.append("organizer never reached waiting_user")
        finally:
            approval_done.set()

    approver_thread = threading.Thread(target=approver, daemon=True)

    try:
        cancel_event = threading.Event()
        context = AgentExecutionContext(
            conversation_id="conv-e2e",
            parent_turn_id="turn-e2e",
            owner_id="main:conv-e2e:turn-e2e:x",
            cancel_event=cancel_event,
        )
        with bind_execution_context(context):
            dispatched = DispatchSubagentsTool(manager).execute(
                tasks=[
                    {"title": "找考研资料", "role": "explorer", "goal": "在 Documents 找考研资料"},
                    {"title": "查今年政策", "role": "researcher", "goal": "调研 2026 考研政策"},
                    {
                        "title": "播放夜曲",
                        "role": "operator",
                        "goal": "在网易云音乐播放周杰伦的夜曲",
                    },
                    {
                        "title": "整理考研资料",
                        "role": "organizer",
                        "goal": "把考研资料移动到 考研 文件夹",
                    },
                ]
            )
            assert dispatched.ok is True
            task_ids = dispatched.data["task_ids"]
            assert len(task_ids) == 4

            approver_thread.start()
            awaited = AwaitSubagentsTool(manager).execute(task_ids=task_ids, timeout_s=30)

        assert approval_done.wait(timeout=15)
        assert not approval_error, approval_error
        assert not barrier_broken.is_set(), "explorer/organizer planning never overlapped"

        # One result per task, exactly the dispatched ids.
        assert awaited.ok is True
        results = {item["task_id"]: item for item in awaited.data["results"]}
        assert set(results) == set(task_ids)
        assert awaited.data["pending"] == []

        by_role = {item["role"]: item for item in results.values()}
        assert by_role["explorer"]["status"] == "completed"
        assert by_role["operator"]["status"] == "completed"
        assert by_role["organizer"]["status"] == "completed"
        assert by_role["researcher"]["status"] == "failed"

        # Injected failure delivered salvage to the main agent — recovery is
        # machine-actionable, never "please paste your progress".
        failed = by_role["researcher"]
        assert failed["partial_result"]["resume_from"] == "run-e2e"
        assert failed["partial_result"]["findings"]
        assert failed["next_action"]

        # Organizer moved the file only after approval.
        assert destination.exists() and not source.exists()

        # Operator owned the desktop exclusively while running.
        operator_task_id = next(
            task_id for task_id, item in results.items() if item["role"] == "operator"
        )
        assert desktop_owner_seen["owner"] == task_owner_id(operator_task_id)
        assert resources.owner(DESKTOP) is None  # released afterwards

        # Every persisted event carries full identity and monotonic sequences.
        for task_id in task_ids:
            events = manager.list_events(task_id)
            assert events, f"no events for {task_id}"
            sequences = [event.sequence for event in events]
            assert sequences == sorted(sequences)
            assert len(sequences) == len(set(sequences))
            for event in events:
                assert event.conversation_id == "conv-e2e"
                assert event.parent_turn_id == "turn-e2e"
                assert event.task_id == task_id
    finally:
        try:
            overlap_barrier.abort()
        except Exception:
            pass
        approver_thread.join(timeout=5)
        manager.shutdown()
        store.close()
