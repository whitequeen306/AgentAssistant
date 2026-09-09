"""Organizer manifest approval + safe apply (Task 17)."""

from __future__ import annotations

import json
import time

import pytest

from agent_assistant.subagents.manager import SubagentManager
from agent_assistant.subagents.manifest import (
    apply_manifest,
    build_manifest,
    canonicalize_moves,
    manifest_hash,
    rollback_applied,
)
from agent_assistant.subagents.models import SubagentRole, SubagentSpec, SubagentStatus
from agent_assistant.subagents.resources import ResourceCoordinator
from agent_assistant.subagents.runners.organizer import organizer_runner
from agent_assistant.subagents.store import SubagentStore


@pytest.fixture()
def jail(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_assistant.tools.permission.jail_roots", lambda: [str(tmp_path)]
    )
    return tmp_path


# ─── Manifest canonicalization + hashing ─────────────────────────────────────


def test_canonicalize_filters_unsafe_proposals(jail, tmp_path_factory) -> None:
    src_ok = jail / "文档.pdf"
    src_ok.write_bytes(b"pdf")
    existing_dst = jail / "已存在.pdf"
    existing_dst.write_bytes(b"x")
    outside = tmp_path_factory.mktemp("outside") / "外面.txt"
    outside.write_text("x", encoding="utf-8")

    build = canonicalize_moves(
        [
            {"src": str(src_ok), "dst": str(jail / "分类" / "文档.pdf")},
            {"src": str(jail / "不存在.txt"), "dst": str(jail / "a.txt")},
            {"src": str(src_ok), "dst": str(existing_dst)},  # dst exists
            {"src": str(outside), "dst": str(jail / "b.txt")},  # outside jail
            {"src": str(src_ok), "dst": str(jail / "重复.pdf")},  # duplicate src
        ]
    )
    assert len(build.operations) == 1
    operation = build.operations[0]
    assert operation["conflict"] == "skip"
    assert operation["source_fingerprint"]["size"] == 3
    assert operation["operation_id"]
    reasons = {item["reason"] for item in build.skipped}
    assert "源文件不存在" in reasons
    assert "目标已存在（不覆盖）" in reasons
    assert "超出允许目录范围" in reasons
    assert "重复的源文件" in reasons


def test_manifest_hash_changes_with_operations(jail) -> None:
    (jail / "a.txt").write_text("a", encoding="utf-8")
    (jail / "b.txt").write_text("b", encoding="utf-8")
    build_one = canonicalize_moves([{"src": str(jail / "a.txt"), "dst": str(jail / "x/a.txt")}])
    build_two = canonicalize_moves(
        [
            {"src": str(jail / "a.txt"), "dst": str(jail / "x/a.txt")},
            {"src": str(jail / "b.txt"), "dst": str(jail / "x/b.txt")},
        ]
    )
    manifest_one = build_manifest("t1", "整理", build_one)
    manifest_two = build_manifest("t1", "整理", build_two)
    assert manifest_hash(manifest_one) != manifest_hash(manifest_two)
    # Hash is stable across timestamp differences (core fields only).
    manifest_one_copy = dict(manifest_one)
    manifest_one_copy["created_at"] = "2020-01-01T00:00:00+00:00"
    assert manifest_hash(manifest_one) == manifest_hash(manifest_one_copy)


def test_apply_rejects_hash_mismatch_and_moves_nothing(jail) -> None:
    src = jail / "a.txt"
    src.write_text("a", encoding="utf-8")
    build = canonicalize_moves([{"src": str(src), "dst": str(jail / "x/a.txt")}])
    manifest = build_manifest("t1", "整理", build)
    from agent_assistant.subagents.manifest import ManifestError

    with pytest.raises(ManifestError):
        apply_manifest(manifest, "wrong-hash")
    assert src.exists()


def test_apply_skips_modified_and_occupied_targets(jail) -> None:
    stable = jail / "stable.txt"
    stable.write_text("s", encoding="utf-8")
    modified = jail / "modified.txt"
    modified.write_text("m", encoding="utf-8")
    blocked = jail / "blocked.txt"
    blocked.write_text("b", encoding="utf-8")

    build = canonicalize_moves(
        [
            {"src": str(stable), "dst": str(jail / "out/stable.txt")},
            {"src": str(modified), "dst": str(jail / "out/modified.txt")},
            {"src": str(blocked), "dst": str(jail / "out/blocked.txt")},
        ]
    )
    manifest = build_manifest("t1", "整理", build)
    digest = manifest_hash(manifest)

    # Post-approval tampering: content grows; destination appears.
    time.sleep(0.01)
    modified.write_text("m-changed-now", encoding="utf-8")
    (jail / "out").mkdir()
    (jail / "out" / "blocked.txt").write_text("occupied", encoding="utf-8")

    report = apply_manifest(manifest, digest)
    assert [item["src"] for item in report.applied] == [str(stable)]
    assert (jail / "out" / "stable.txt").exists()
    assert modified.exists()  # untouched
    reasons = {item["reason"] for item in report.skipped}
    assert "源文件在批准后被修改" in reasons
    assert "目标已存在（不覆盖）" in reasons
    assert (jail / "out" / "blocked.txt").read_text(encoding="utf-8") == "occupied"


def test_rollback_refuses_overwrite_and_modified_files(jail) -> None:
    src_a = jail / "a.txt"
    src_a.write_text("aa", encoding="utf-8")
    src_b = jail / "b.txt"
    src_b.write_text("bb", encoding="utf-8")
    build = canonicalize_moves(
        [
            {"src": str(src_a), "dst": str(jail / "out/a.txt")},
            {"src": str(src_b), "dst": str(jail / "out/b.txt")},
        ]
    )
    manifest = build_manifest("t1", "整理", build)
    report = apply_manifest(manifest, manifest_hash(manifest))
    assert len(report.applied) == 2

    # Tamper: modify moved a; occupy b's original slot.
    (jail / "out" / "a.txt").write_text("aa-changed", encoding="utf-8")
    src_b.write_text("new file at original path", encoding="utf-8")

    rollback = rollback_applied(report.applied)
    reasons = {item["reason"] for item in rollback.skipped}
    assert "文件在整理后被修改" in reasons
    assert "原位置已被占用（不覆盖）" in reasons
    assert rollback.applied == []
    assert (jail / "out" / "b.txt").exists()  # untouched


def test_rollback_moves_untouched_files_back(jail) -> None:
    src = jail / "a.txt"
    src.write_text("aa", encoding="utf-8")
    build = canonicalize_moves([{"src": str(src), "dst": str(jail / "out/a.txt")}])
    manifest = build_manifest("t1", "整理", build)
    report = apply_manifest(manifest, manifest_hash(manifest))
    assert not src.exists()

    rollback = rollback_applied(report.applied)
    assert len(rollback.applied) == 1
    assert src.exists()
    assert not (jail / "out" / "a.txt").exists()


# ─── Plan-phase registry ─────────────────────────────────────────────────────


def test_plan_registry_has_no_write_tools() -> None:
    from agent_assistant.subagents.runners.organizer import _build_plan_registry

    registry = _build_plan_registry({})
    names = {tool.name for tool in registry.all_tools()}
    assert "move_file" not in names
    assert "write_file" not in names
    assert "save_note" not in names
    assert "propose_moves" in names
    assert "search_local_files" in names


# ─── End-to-end through the manager ──────────────────────────────────────────


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


def _organizer_env(jail, tmp_path, monkeypatch, moves):
    """Manager + scripted LLM that proposes ``moves`` then summarizes."""
    from agent_assistant.config import settings as app_settings
    from agent_assistant.subagents import runtime as runtime_module

    monkeypatch.setattr(app_settings, "data_dir", tmp_path / "data")

    class PlanLLM:
        def __init__(self):
            self.turn = 0

        def chat(self, messages, tools=None):
            self.turn += 1
            if self.turn == 1:
                return _Response(
                    _Message(tool_calls=[_Call("c1", "propose_moves", {"moves": moves})])
                )
            return _Response(_Message(content="计划：按主题移动文件到分类目录。"))

    monkeypatch.setattr(runtime_module, "llm_client", PlanLLM())

    store = SubagentStore(tmp_path / "data" / "subagents.db")
    manager = SubagentManager(
        store,
        runner_for=lambda role: organizer_runner,
        archive_root=tmp_path / "data" / "subagent_runs",
        resources=ResourceCoordinator(),
    )
    spec = SubagentSpec(
        task_id="org-e2e",
        conversation_id="conv-1",
        parent_turn_id="turn-1",
        role=SubagentRole.ORGANIZER,
        title="整理考研资料",
        goal="把考研资料移动到 考研 文件夹",
    )
    return manager, store, spec


def _wait_status(manager, task_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.get_task(task_id)
        if task is not None and task.status is status:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task never reached {status}")


def test_organizer_plan_approval_apply_flow(jail, tmp_path, monkeypatch) -> None:
    source = jail / "考研英语.pdf"
    source.write_bytes(b"pdf-bytes")
    destination = jail / "考研" / "考研英语.pdf"
    moves = [{"src": str(source), "dst": str(destination)}]

    manager, store, spec = _organizer_env(jail, tmp_path, monkeypatch, moves)
    try:
        manager.dispatch([spec])
        _wait_status(manager, "org-e2e", SubagentStatus.WAITING_USER)

        # Nothing moved during planning; manifest persisted with hash.
        assert source.exists() and not destination.exists()
        events = manager.list_events("org-e2e")
        waiting_events = [
            event for event in events if event.payload.get("status") == "waiting_user"
        ]
        assert waiting_events
        prompt = waiting_events[-1].payload
        assert prompt["moves"] == 1
        digest = prompt["manifest_hash"]
        assert digest

        # Wrong hash rejected.
        wrong = manager.respond_organizer_plan("org-e2e", "deadbeef", True)
        assert wrong["ok"] is False

        # Correct hash approved → deterministic apply.
        approved = manager.respond_organizer_plan("org-e2e", digest, True)
        assert approved == {"ok": True, "approved": True}
        results = manager.await_tasks(["org-e2e"], timeout=10)
        assert len(results) == 1
        assert results[0].status is SubagentStatus.COMPLETED
        assert destination.exists() and not source.exists()
        assert results[0].stats["moved"] == 1

        # Approval is single-use.
        again = manager.respond_organizer_plan("org-e2e", digest, True)
        assert again["ok"] is False
    finally:
        manager.shutdown()
        store.close()


def test_organizer_denial_cancels_without_moving(jail, tmp_path, monkeypatch) -> None:
    source = jail / "笔记.md"
    source.write_text("note", encoding="utf-8")
    moves = [{"src": str(source), "dst": str(jail / "分类" / "笔记.md")}]

    manager, store, spec = _organizer_env(jail, tmp_path, monkeypatch, moves)
    try:
        manager.dispatch([spec])
        _wait_status(manager, "org-e2e", SubagentStatus.WAITING_USER)
        events = manager.list_events("org-e2e")
        digest = next(
            event.payload["manifest_hash"]
            for event in reversed(events)
            if event.payload.get("manifest_hash")
        )
        denied = manager.respond_organizer_plan("org-e2e", digest, False)
        assert denied == {"ok": True, "approved": False}
        results = manager.await_tasks(["org-e2e"], timeout=5)
        assert results[0].status is SubagentStatus.CANCELLED
        assert source.exists()
        # Manifest preserved on disk for audit.
        from agent_assistant.subagents.archive import TaskArchive

        archive = TaskArchive(tmp_path / "data" / "subagent_runs", "org-e2e")
        checkpoint = archive.read_checkpoint()
        assert checkpoint["phase"] == "denied"
        assert checkpoint["manifest"]["operations"]
    finally:
        manager.shutdown()
        store.close()


def test_expired_approval_cancels_task(jail, tmp_path, monkeypatch) -> None:
    source = jail / "旧文件.txt"
    source.write_text("old", encoding="utf-8")
    moves = [{"src": str(source), "dst": str(jail / "归档" / "旧文件.txt")}]

    manager, store, spec = _organizer_env(jail, tmp_path, monkeypatch, moves)
    try:
        manager.dispatch([spec])
        _wait_status(manager, "org-e2e", SubagentStatus.WAITING_USER)
        events = manager.list_events("org-e2e")
        digest = next(
            event.payload["manifest_hash"]
            for event in reversed(events)
            if event.payload.get("manifest_hash")
        )
        monkeypatch.setattr(
            "agent_assistant.subagents.manifest.approval_expired",
            lambda manifest, now=None: True,
        )
        expired = manager.respond_organizer_plan("org-e2e", digest, True)
        assert expired["ok"] is False
        results = manager.await_tasks(["org-e2e"], timeout=5)
        assert results[0].status is SubagentStatus.CANCELLED
        assert source.exists()
    finally:
        manager.shutdown()
        store.close()


def test_bridge_respond_organizer_plan_routes_to_manager(jail, tmp_path, monkeypatch) -> None:
    from agent_assistant.ui.bridge import ApiBridge

    source = jail / "资料.txt"
    source.write_text("data", encoding="utf-8")
    moves = [{"src": str(source), "dst": str(jail / "资料库" / "资料.txt")}]
    manager, store, spec = _organizer_env(jail, tmp_path, monkeypatch, moves)
    try:
        manager.dispatch([spec])
        _wait_status(manager, "org-e2e", SubagentStatus.WAITING_USER)
        events = manager.list_events("org-e2e")
        digest = next(
            event.payload["manifest_hash"]
            for event in reversed(events)
            if event.payload.get("manifest_hash")
        )
        bridge = ApiBridge()
        bridge._active_conv_id = "conv-1"
        bridge.set_subagent_manager(manager)
        response = bridge.respond_organizer_plan("org-e2e", digest, True)
        assert response["ok"] is True
        results = manager.await_tasks(["org-e2e"], timeout=10)
        assert results[0].status is SubagentStatus.COMPLETED
        assert (jail / "资料库" / "资料.txt").exists()
    finally:
        manager.shutdown()
        store.close()
