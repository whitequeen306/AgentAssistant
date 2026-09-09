"""Main-agent orchestration tools: dispatch / await / control subagents.

Identity is NEVER taken from model arguments — the bound execution context
(Task 2) supplies conversation_id / parent_turn_id, and every await/control
target is verified to belong to that conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_assistant.subagents.context import current_execution_context
from agent_assistant.subagents.models import (
    SubagentRole,
    SubagentSpec,
)
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

MAX_PARALLEL_TASKS = 4
_CONTROL_ACTIONS = ("continue", "retry", "pause", "resume", "cancel")
_MAX_AWAIT_TIMEOUT_S = 7200
_DEFAULT_AWAIT_TIMEOUT_S = 1800


def _get_manager(injected: Any):
    if injected is not None:
        return injected
    from agent_assistant.subagents.service import get_manager

    return get_manager()


def _require_context() -> tuple[Any, ToolResult | None]:
    context = current_execution_context()
    if context is None:
        return None, ToolResult.failure(
            "subagent orchestration requires a bound conversation turn; "
            "this call has no execution context",
            code=409,
            error_category="missing_execution_context",
        )
    return context, None


class DispatchSubagentsTool(Tool):
    """Create up to four subagent tasks and return their IDs immediately."""

    def __init__(self, manager: Any = None) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "dispatch_subagents"

    @property
    def description(self) -> str:
        return (
            "Dispatch 1-4 subagent tasks that run in parallel while you keep "
            "working. Roles: explorer (find/read LOCAL files & notes, "
            "read-only), researcher (iterative WEB research producing a "
            "sourced report), operator (drive desktop apps via UI automation; "
            "only ONE operator can run at a time), organizer (plan file moves "
            "for user approval, then apply). Each task needs a short title, a "
            "role, and a self-contained goal — the subagent cannot see this "
            "conversation, so put everything it needs in goal/context. "
            "After dispatching, call await_subagents to collect results. "
            "Do NOT dispatch for trivial one-step work you can do directly, "
            "and never dispatch a subagent from inside a subagent."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="tasks",
                type="array",
                description=(
                    "Tasks to dispatch (1-4). Each item: "
                    "{title, role: explorer|researcher|operator|organizer, "
                    "goal, context?}"
                ),
                schema={
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PARALLEL_TASKS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Short display title (UI task row)",
                            },
                            "role": {
                                "type": "string",
                                "enum": [role.value for role in SubagentRole],
                                "description": "Which specialist runs this task",
                            },
                            "goal": {
                                "type": "string",
                                "description": "Self-contained goal for the subagent",
                            },
                            "context": {
                                "type": "string",
                                "description": "Optional background the subagent needs",
                            },
                        },
                        "required": ["title", "role", "goal"],
                    },
                },
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        context, error = _require_context()
        if error is not None:
            return error
        tasks = kwargs.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return ToolResult.failure("parameter 'tasks' must be a non-empty array", code=400)
        if len(tasks) > MAX_PARALLEL_TASKS:
            return ToolResult.failure(
                f"at most {MAX_PARALLEL_TASKS} tasks per dispatch", code=400
            )

        specs: list[SubagentSpec] = []
        for index, item in enumerate(tasks):
            if not isinstance(item, dict):
                return ToolResult.failure(f"tasks[{index}] must be an object", code=400)
            try:
                spec_context: dict[str, Any] = {}
                background = str(item.get("context") or "").strip()
                if background:
                    spec_context["background"] = background
                specs.append(
                    SubagentSpec.create(
                        conversation_id=context.conversation_id,
                        parent_turn_id=context.parent_turn_id,
                        role=str(item.get("role") or ""),
                        title=str(item.get("title") or ""),
                        goal=str(item.get("goal") or ""),
                        context=spec_context,
                    )
                )
            except ValueError as exc:
                return ToolResult.failure(f"tasks[{index}] is invalid: {exc}", code=400)

        try:
            manager = _get_manager(self._manager)
            task_ids = manager.dispatch(specs)
        except Exception:
            logger.exception("dispatch_subagents failed")
            return ToolResult.failure(
                "failed to dispatch subagent tasks; the role may not be "
                "available in this build",
                code=500,
                error_category="dispatch_failed",
            )
        return ToolResult.success(
            data={
                "task_ids": task_ids,
                "tasks": [
                    {"task_id": spec.task_id, "title": spec.title, "role": spec.role.value}
                    for spec in specs
                ],
                "hint": "call await_subagents with these task_ids to collect results",
            }
        )


class AwaitSubagentsTool(Tool):
    """Block until the listed tasks reach a terminal state, then return results."""

    def __init__(self, manager: Any = None) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "await_subagents"

    @property
    def description(self) -> str:
        return (
            "Wait for dispatched subagent tasks to finish and return their "
            "unified results (summary, output, evidence, artifacts, partial "
            "progress on failure). Call this ONCE with all task_ids from "
            "dispatch_subagents — never poll in a loop. Failed tasks still "
            "return salvaged partial_result; decide yourself whether to "
            "retry/continue via control_subagent or finish the gap directly. "
            "Do not ask the user to relay progress."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="task_ids",
                type="array",
                description="Task IDs returned by dispatch_subagents",
                schema={
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            ),
            ToolParameter(
                name="timeout_s",
                type="integer",
                description=(
                    f"Max seconds to wait (default {_DEFAULT_AWAIT_TIMEOUT_S}, "
                    f"cap {_MAX_AWAIT_TIMEOUT_S})"
                ),
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        context, error = _require_context()
        if error is not None:
            return error
        task_ids = kwargs.get("task_ids")
        if not isinstance(task_ids, list) or not task_ids:
            return ToolResult.failure("parameter 'task_ids' must be a non-empty array", code=400)
        if not all(isinstance(task_id, str) and task_id.strip() for task_id in task_ids):
            return ToolResult.failure("every task_id must be a non-empty string", code=400)
        timeout_raw = kwargs.get("timeout_s", _DEFAULT_AWAIT_TIMEOUT_S)
        try:
            timeout_s = min(max(int(timeout_raw), 1), _MAX_AWAIT_TIMEOUT_S)
        except (TypeError, ValueError):
            return ToolResult.failure("timeout_s must be an integer", code=400)

        manager = _get_manager(self._manager)
        foreign = _foreign_tasks(manager, task_ids, context.conversation_id)
        if foreign is not None:
            return foreign

        results = manager.await_tasks(
            list(task_ids),
            timeout=timeout_s,
            cancel_check=context.cancel_event.is_set,
        )
        result_ids = {result.task_id for result in results}
        pending = [task_id for task_id in task_ids if task_id not in result_ids]
        if pending and context.cancel_event.is_set():
            return ToolResult.failure(
                "turn cancelled while waiting for subagents; partial results "
                "included in data",
                code=499,
                error_category="user_cancelled",
            )
        return ToolResult.success(
            data={
                "results": [result.to_dict() for result in results],
                "pending": pending,
            },
            warning=(
                "some tasks are still running; await again with the pending ids"
                if pending
                else None
            ),
        )


class ControlSubagentTool(Tool):
    """Lifecycle control for one subagent task."""

    def __init__(self, manager: Any = None) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "control_subagent"

    @property
    def description(self) -> str:
        return (
            "Control one subagent task: cancel (stop it, progress preserved), "
            "pause (Operator only — yields the desktop at a safe point), "
            "resume (continue a paused task), retry (re-run a "
            "failed/cancelled/interrupted task, attempt+1), continue "
            "(follow-up instruction for a completed/failed task, attempt+1; "
            "requires 'instruction'). After retry/continue/resume, call "
            "await_subagents with the same task_id."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="task_id", type="string", description="Target task ID"),
            ToolParameter(
                name="action",
                type="string",
                description="Lifecycle action",
                enum=list(_CONTROL_ACTIONS),
            ),
            ToolParameter(
                name="instruction",
                type="string",
                description="Follow-up instruction (required for 'continue')",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        context, error = _require_context()
        if error is not None:
            return error
        task_id = str(kwargs.get("task_id") or "").strip()
        action = str(kwargs.get("action") or "").strip()
        instruction = kwargs.get("instruction")
        if not task_id:
            return ToolResult.failure("parameter 'task_id' is required", code=400)
        if action not in _CONTROL_ACTIONS:
            return ToolResult.failure(
                f"action must be one of {', '.join(_CONTROL_ACTIONS)}", code=400
            )

        manager = _get_manager(self._manager)
        foreign = _foreign_tasks(manager, [task_id], context.conversation_id)
        if foreign is not None:
            return foreign
        try:
            spec = manager.control(
                task_id,
                action=action,
                instruction=str(instruction) if instruction is not None else None,
            )
        except KeyError:
            return ToolResult.failure(
                "task not found in this conversation", code=404,
                error_category="task_not_found",
            )
        except ValueError as exc:
            return ToolResult.failure(str(exc), code=400)
        return ToolResult.success(
            data={"task": spec.to_dict(), "action": action}
        )


def _foreign_tasks(manager: Any, task_ids: list[str], conversation_id: str) -> ToolResult | None:
    """Reject IDs that don't exist or belong to another conversation.

    Both cases return the same opaque failure so foreign task existence is
    not observable.
    """
    for task_id in task_ids:
        spec = manager.get_task(task_id)
        if spec is None or spec.conversation_id != conversation_id:
            return ToolResult.failure(
                "task not found in this conversation",
                code=404,
                error_category="task_not_found",
            )
    return None
