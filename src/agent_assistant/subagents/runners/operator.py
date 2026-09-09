"""Operator runner: exclusive desktop UI automation with inspect-act-verify.

The desktop lock itself is acquired by the SubagentManager before the task
starts (profile ``needs_desktop``); this module enforces the behavioral
contract on top of it:

- a mutating action (click / type / hotkey) is refused until the UI has been
  inspected since the last screen-changing action, and
- every successful mutation invalidates the observation, forcing a re-inspect
  before the next mutation (post-action verification).

Typed text never reaches events or the L0 archive: progress labels never carry
tool arguments, and the archive redacts ``ui_type.text`` recursively.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_assistant.tools.base import ToolResult
from agent_assistant.tools.registry import ToolRegistry

from ..archive import TaskArchive, TaskArchiveError
from ..manager import EmitFn, PausedOutcome, TaskControls
from ..models import SubagentResult, SubagentRole, SubagentSpec
from ..profiles import profile_for
from ..runtime import run_task_loop

logger = logging.getLogger(__name__)

# Actions that change application state and therefore require a fresh
# observation first. Scrolling only changes the viewport and is allowed —
# it is usually needed to bring the target INTO view before inspecting.
MUTATING_UI_TOOLS = frozenset({"ui_click", "ui_type", "ui_hotkey"})
# Actions after which the previous UI observation is stale.
SCREEN_CHANGING_TOOLS = MUTATING_UI_TOOLS | frozenset(
    {"launch_app", "focus_window", "ui_scroll"}
)


class OperatorRegistry(ToolRegistry):
    """Tool registry that enforces inspect-before-act for the Operator."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._inspected = False

    def execute(self, name: str, arguments: Any) -> ToolResult:
        if name in MUTATING_UI_TOOLS and not self._inspected:
            return ToolResult.failure(
                "UI not observed yet: call ui_inspect first (the screen may "
                "have changed since your last look), verify the target "
                "element, then act.",
                error_category="inspect_required",
            )
        result = super().execute(name, arguments)
        if name == "ui_inspect" and result.ok:
            self._inspected = True
        elif name in SCREEN_CHANGING_TOOLS and result.ok:
            # The action (probably) changed the screen — force re-observation
            # before the next mutation so success is verified, not assumed.
            self._inspected = False
        return result


def _build_registry() -> OperatorRegistry:
    from agent_assistant.tools.focus_window import FocusWindowTool
    from agent_assistant.tools.launch_app import LaunchAppTool
    from agent_assistant.tools.ui_automation import (
        UiClickTool,
        UiHotkeyTool,
        UiInspectTool,
        UiScrollTool,
        UiTypeTool,
    )

    registry = OperatorRegistry()
    registry.register(LaunchAppTool())
    registry.register(FocusWindowTool())
    registry.register(UiInspectTool())
    registry.register(UiClickTool())
    registry.register(UiTypeTool())
    registry.register(UiHotkeyTool())
    registry.register(UiScrollTool())

    profile = profile_for(SubagentRole.OPERATOR)
    registered = {tool.name for tool in registry.all_tools()}
    assert registered == set(profile.tools), "operator registry drifted from profile"
    return registry


def _evidence(tool: str, args: dict[str, Any], result_dict: dict[str, Any]) -> list[dict]:
    """Sanitized action trail: action + target descriptor + state hash, no text."""
    if tool not in SCREEN_CHANGING_TOOLS and tool != "ui_inspect":
        return []
    target = ""
    for field in ("target", "title_pattern", "name"):
        value = args.get(field)
        if isinstance(value, str) and value:
            target = value[:80]
            break
    data = result_dict.get("data")
    state_hash = ""
    if isinstance(data, dict):
        state_hash = str(data.get("state_hash") or "")[:16]
    claim = f"{tool} 执行成功" if result_dict.get("ok") else f"{tool} 未生效"
    if state_hash:
        claim += f"（界面指纹 {state_hash}）"
    return [{"type": "ui_action", "ref": target, "claim": claim}]


def operator_runner(
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
        logger.warning("Operator archive unavailable for %s", spec.task_id[:8])

    profile = profile_for(SubagentRole.OPERATOR)
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
