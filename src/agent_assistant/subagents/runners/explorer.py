"""Explorer runner: read-only local exploration (files, notes, knowledge)."""

from __future__ import annotations

import logging
from typing import Any

from ..archive import TaskArchive, TaskArchiveError
from ..manager import EmitFn, PausedOutcome, TaskControls
from ..models import SubagentResult, SubagentRole, SubagentSpec
from ..profiles import profile_for
from ..runtime import run_task_loop

logger = logging.getLogger(__name__)


def _build_registry():
    """Fresh allowlisted registry — Explorer never sees write/desktop tools."""
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

    profile = profile_for(SubagentRole.EXPLORER)
    registered = {tool.name for tool in registry.all_tools()}
    assert registered == set(profile.tools), "explorer registry drifted from profile"
    return registry


def _evidence(tool: str, args: dict[str, Any], result_dict: dict[str, Any]) -> list[dict]:
    """Path-backed evidence. read_file → verified; search/list hits → found."""
    data = result_dict.get("data")
    if not isinstance(data, dict):
        return []
    if tool == "read_file" and args.get("path"):
        return [{"type": "file_read", "ref": str(args["path"]), "claim": "内容已读取核实"}]
    if tool == "search_local_files":
        return [
            {
                "type": "file_found",
                "ref": str(item.get("path", "")),
                "claim": f"匹配（{item.get('match', 'name')}），未读取" ,
            }
            for item in (data.get("results") or [])[:10]
            if isinstance(item, dict) and item.get("path")
        ]
    return []


def explorer_runner(
    spec: SubagentSpec,
    controls: TaskControls,
    emit: EmitFn,
) -> SubagentResult | PausedOutcome:
    from agent_assistant.config import settings

    archive: TaskArchive | None
    try:
        archive = TaskArchive(settings.subagent_runs_dir, spec.task_id)
    except (TaskArchiveError, ValueError):
        archive = None
        logger.warning("Explorer archive unavailable for %s", spec.task_id[:8])

    profile = profile_for(SubagentRole.EXPLORER)
    return run_task_loop(
        spec,
        controls,
        emit,
        registry=_build_registry(),
        system_prompt=profile.system_prompt,
        max_turns=profile.max_turns,
        archive=archive,
        evidence_extractor=_evidence,
    )
