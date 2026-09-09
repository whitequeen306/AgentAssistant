"""Organizer runner: plan (read-only) → user approval → deterministic apply.

Phase 1 (plan): a read-only exploration loop whose only "write" is the
``propose_moves`` capture tool. The proposal is canonicalized into a hashed
manifest, persisted to the task checkpoint, and the task parks in
``waiting_user``.

Phase 2 (apply): NO LLM involved — after approval the manager requeues the
task with ``organizer_phase=apply`` and this runner executes exactly the
approved manifest through ``manifest.apply_manifest`` (revalidating jail,
fingerprints, destination absence, and the hash).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult

from ..archive import TaskArchive, TaskArchiveError
from ..manager import EmitFn, PausedOutcome, TaskControls, WaitingUserOutcome
from ..manifest import (
    ManifestError,
    apply_manifest,
    build_manifest,
    canonicalize_moves,
    manifest_hash,
)
from ..models import SubagentResult, SubagentRole, SubagentSpec, SubagentStatus
from ..profiles import profile_for
from ..runtime import run_task_loop

logger = logging.getLogger(__name__)

_PLAN_EXTRA_PROMPT = """

Planning protocol (mandatory):
1. Explore with the read-only tools until you know exactly which files to move.
2. Call propose_moves ONCE with the complete move list (absolute src/dst).
3. Then write a short human summary: grouping criteria, move count, notable skips.
You cannot move anything yourself — the user approves the manifest first."""


class ProposeMovesTool(Tool):
    """Captures the plan; performs no filesystem mutation."""

    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder

    @property
    def name(self) -> str:
        return "propose_moves"

    @property
    def description(self) -> str:
        return (
            "Submit the COMPLETE file-move plan for user approval. Call once, "
            "after exploring. Each move needs absolute src and dst. Nothing "
            "is moved now — the user reviews and approves the manifest first."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="moves",
                type="array",
                description="Complete move list",
                schema={
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "src": {"type": "string", "description": "Absolute source path"},
                            "dst": {"type": "string", "description": "Absolute destination path"},
                        },
                        "required": ["src", "dst"],
                    },
                },
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        moves = kwargs.get("moves")
        if not isinstance(moves, list) or not moves:
            return ToolResult.failure("parameter 'moves' must be a non-empty array", code=400)
        build = canonicalize_moves(moves)
        if not build.operations and not build.skipped:
            return ToolResult.failure("no valid moves in the proposal", code=400)
        self._holder["build"] = build
        return ToolResult.success(
            data={
                "accepted": len(build.operations),
                "skipped": len(build.skipped),
                "skip_reasons": build.skipped[:10],
                "next": "now write the human summary of this plan",
            }
        )


def _build_plan_registry(holder: dict[str, Any]):
    from agent_assistant.tools.file_tools import ListFilesTool, ReadFileTool
    from agent_assistant.tools.knowledge_tools import (
        ReadAttachedSourceTool,
        SearchKnowledgeTool,
    )
    from agent_assistant.tools.registry import ToolRegistry
    from agent_assistant.tools.search_local_files import SearchLocalFilesTool

    registry = ToolRegistry()
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(SearchLocalFilesTool())
    registry.register(SearchKnowledgeTool())
    registry.register(ReadAttachedSourceTool())
    registry.register(ProposeMovesTool(holder))

    profile = profile_for(SubagentRole.ORGANIZER)
    registered = {tool.name for tool in registry.all_tools()}
    expected = (set(profile.tools) - {"move_file", "save_note"}) | {"propose_moves"}
    assert registered == expected, "organizer plan registry drifted from profile"
    return registry


def _archive_for(spec: SubagentSpec) -> TaskArchive | None:
    from agent_assistant.config import settings

    try:
        return TaskArchive(settings.subagent_runs_dir, spec.task_id)
    except (TaskArchiveError, ValueError):
        logger.warning("Organizer archive unavailable for %s", spec.task_id[:8])
        return None


def organizer_runner(
    spec: SubagentSpec,
    controls: TaskControls,
    emit: EmitFn,
) -> SubagentResult | PausedOutcome | WaitingUserOutcome:
    if str(spec.context.get("organizer_phase") or "") == "apply":
        return _apply_phase(spec, emit)
    return _plan_phase(spec, controls, emit)


def _plan_phase(
    spec: SubagentSpec,
    controls: TaskControls,
    emit: EmitFn,
) -> SubagentResult | PausedOutcome | WaitingUserOutcome:
    holder: dict[str, Any] = {}
    profile = profile_for(SubagentRole.ORGANIZER)
    archive = _archive_for(spec)

    outcome = run_task_loop(
        spec,
        controls,
        emit,
        registry=_build_plan_registry(holder),
        system_prompt=profile.system_prompt + _PLAN_EXTRA_PROMPT,
        max_turns=profile.max_turns,
        archive=archive,
    )
    if isinstance(outcome, PausedOutcome):
        return outcome
    if outcome.status is not SubagentStatus.COMPLETED:
        return outcome  # cancelled / failed planning — progress already salvaged

    build = holder.get("build")
    if build is None or not build.operations:
        # Nothing to move (already organized, or only skips) — legitimate end.
        return outcome

    manifest = build_manifest(spec.task_id, spec.goal, build)
    digest = manifest_hash(manifest)
    checkpoint = {
        "phase": "awaiting_approval",
        "manifest": manifest,
        "manifest_hash": digest,
        "summary": str(outcome.output or "")[:4000],
    }
    destinations = sorted({op["dst"].rsplit("\\", 1)[0] for op in manifest["operations"]})[:8]
    prompt = {
        "label": f"整理计划待确认：{len(manifest['operations'])} 个移动",
        "manifest_hash": digest,
        "moves": len(manifest["operations"]),
        "skipped": len(manifest["skipped"]),
        "destinations": destinations,
        "operations_preview": manifest["operations"][:50],
        "skips_preview": manifest["skipped"][:20],
        "plan_summary": str(outcome.output or "")[:2000],
    }
    return WaitingUserOutcome(checkpoint=checkpoint, prompt=prompt)


def _apply_phase(spec: SubagentSpec, emit: EmitFn) -> SubagentResult:
    archive = _archive_for(spec)
    checkpoint = None
    if archive is not None:
        try:
            checkpoint = archive.read_checkpoint()
        except TaskArchiveError:
            checkpoint = None
    expected_hash = str(spec.context.get("manifest_hash") or "")
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("phase") != "applying"
        or not expected_hash
        or str(checkpoint.get("manifest_hash") or "") != expected_hash
    ):
        return SubagentResult.failure(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="整理执行中止：批准状态或计划内容不一致",
            error_category="manifest_mismatch",
            error="approved manifest not found or hash mismatch; nothing was moved",
        )

    manifest = checkpoint.get("manifest") or {}
    emit("activity", {"label": f"开始执行整理：{len(manifest.get('operations') or [])} 个移动"})
    try:
        report = apply_manifest(manifest, expected_hash)
    except ManifestError as exc:
        return SubagentResult.failure(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="整理执行中止：计划校验失败",
            error_category="manifest_mismatch",
            error=str(exc),
        )

    checkpoint["phase"] = "applied"
    checkpoint["apply_report"] = report.to_dict()
    if archive is not None:
        try:
            archive.write_checkpoint(checkpoint)
        except TaskArchiveError:
            logger.warning("Failed to persist apply report for %s", spec.task_id[:8])

    lines = [
        "# 整理完成",
        "",
        f"已移动 {len(report.applied)} 个文件，跳过 {len(report.skipped)} 个。",
        "",
    ]
    for item in report.applied[:50]:
        lines.append(f"- {item['src']} → {item['dst']}")
    if report.skipped:
        lines.append("")
        lines.append("## 跳过")
        for item in report.skipped[:20]:
            lines.append(f"- {item['src']}：{item['reason']}")
    output = "\n".join(lines)
    if archive is not None:
        try:
            archive.write_output(output)
        except TaskArchiveError:
            pass
    emit("activity", {"label": f"整理完成：移动 {len(report.applied)}，跳过 {len(report.skipped)}"})
    return SubagentResult.completed(
        task_id=spec.task_id,
        role=spec.role,
        attempt=spec.attempt,
        summary=f"整理完成：移动 {len(report.applied)} 个文件，跳过 {len(report.skipped)} 个",
        output=output,
        artifacts={"rollback_available": bool(report.applied)},
        evidence=[
            {"type": "file_moved", "ref": item["dst"], "claim": f"来自 {item['src']}"}
            for item in report.applied[:20]
        ],
        stats={
            "moved": len(report.applied),
            "skipped": len(report.skipped),
        },
        l0_raw=str(archive.raw_path) if archive else None,
        l1_trace=str(archive.trace_path) if archive else None,
    )
