"""Generic task-focused subagent runtime (Explorer / Operator / Organizer).

One synchronous LLM+tool loop per task, with:
- the task's own cancellation/pause tokens (no process-global state),
- an execution context bound for the whole run so the tool registry's
  resource guard recognizes this task as the resource owner,
- L0 archival of every tool call BEFORE in-context truncation,
- context aging (recent tool results stay verbatim; older ones collapse to
  one-line stubs; the full text is always in ``raw.jsonl``),
- a forced no-tools wrap-up turn so a final answer always exists.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from agent_assistant.llm.client import llm_client
from agent_assistant.tools.registry import ToolRegistry

from .archive import TaskArchive, TaskArchiveError
from .context import AgentExecutionContext, bind_execution_context
from .manager import EmitFn, PausedOutcome, TaskControls, task_owner_id
from .models import SubagentResult, SubagentSpec

logger = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 120
_TOOL_RESULT_MAX_CHARS = 6000
_KEEP_RECENT_TOOL_MSGS = 8
_WRAP_UP_REMAINING = 2

# Role runners can attach an evidence extractor: (tool, args, result_dict) →
# evidence entries [{type, ref, claim}].
EvidenceExtractor = Callable[[str, dict[str, Any], dict[str, Any]], list[dict[str, Any]]]


def _guarded_llm_call(
    llm: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    cancel_check: Callable[[], bool],
    timeout_s: float = _LLM_TIMEOUT_S,
) -> tuple[Any, str | None]:
    """Blocking LLM call in a daemon thread with cancel polling + timeout."""
    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["response"] = llm.chat(messages=messages, tools=tools)
        except Exception as exc:  # noqa: BLE001 — surfaced as error string
            holder["error"] = str(exc)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    elapsed = 0.0
    while thread.is_alive() and elapsed < timeout_s:
        if cancel_check():
            return None, "cancelled"
        thread.join(timeout=0.25)
        elapsed += 0.25
    if thread.is_alive():
        return None, "timeout"
    if "error" in holder:
        return None, holder["error"]
    return holder.get("response"), None


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw) if raw and raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _tool_summary(tool: str, args: dict[str, Any], result_dict: dict[str, Any]) -> str:
    """One-line L1 summary used both for the trace and for aged-off stubs."""
    ok = result_dict.get("ok", False)
    key = ""
    for field in ("root", "path", "query", "url", "name", "src"):
        value = args.get(field)
        if isinstance(value, str) and value:
            key = value[:80]
            break
    data = result_dict.get("data")
    detail = ""
    if isinstance(data, dict):
        if isinstance(data.get("count"), int):
            detail = f" count={data['count']}"
        elif isinstance(data.get("content"), str):
            detail = f" chars={len(data['content'])}"
    return f"{tool}({key}) → {'ok' if ok else 'fail'}{detail}"


def _age_off_tool_results(
    messages: list[dict[str, Any]],
    summaries: dict[int, str],
) -> None:
    """Collapse all but the newest tool results to one-line stubs (in place)."""
    tool_positions = [
        index for index, message in enumerate(messages) if message.get("role") == "tool"
    ]
    if len(tool_positions) <= _KEEP_RECENT_TOOL_MSGS:
        return
    for index in tool_positions[:-_KEEP_RECENT_TOOL_MSGS]:
        message = messages[index]
        if message.get("_stubbed"):
            continue
        summary = summaries.get(index, "older tool result")
        message["content"] = json.dumps(
            {"stubbed": True, "summary": summary, "note": "full output in raw.jsonl"},
            ensure_ascii=False,
        )
        message["_stubbed"] = True


def _strip_private_keys(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal bookkeeping keys before an API call."""
    cleaned = []
    for message in messages:
        if "_stubbed" in message:
            message = {k: v for k, v in message.items() if k != "_stubbed"}
        cleaned.append(message)
    return cleaned


def run_task_loop(
    spec: SubagentSpec,
    controls: TaskControls,
    emit: EmitFn,
    *,
    registry: ToolRegistry,
    system_prompt: str,
    max_turns: int,
    archive: TaskArchive | None = None,
    llm: Any = None,
    evidence_extractor: EvidenceExtractor | None = None,
) -> SubagentResult | PausedOutcome:
    """Run one task to a terminal result (or a cooperative pause)."""
    llm = llm if llm is not None else llm_client
    tools = registry.openai_schemas()

    goal_lines = [f"Task goal: {spec.goal}"]
    background = str(spec.context.get("background") or "").strip()
    if background:
        goal_lines.append(f"\nBackground:\n{background}")
    continue_instruction = str(spec.context.get("continue_instruction") or "").strip()
    if continue_instruction:
        goal_lines.append(f"\nFollow-up instruction from the user: {continue_instruction}")
    checkpoint = _read_checkpoint(archive)
    if checkpoint:
        digest = json.dumps(checkpoint, ensure_ascii=False)[:2000]
        goal_lines.append(
            "\nA previous attempt of this task was paused/interrupted. "
            f"Checkpoint (continue, do not restart):\n{digest}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(goal_lines)},
    ]
    summaries: dict[int, str] = {}
    evidence: list[dict[str, Any]] = []
    activity: list[str] = []
    wrapup_injected = False

    execution_context = AgentExecutionContext(
        conversation_id=spec.conversation_id,
        parent_turn_id=spec.parent_turn_id,
        owner_id=task_owner_id(spec.task_id),
        cancel_event=controls.cancel_event,
    )

    def partial() -> dict[str, Any]:
        return {
            "turns_used": len(activity),
            "recent_activity": activity[-10:],
            "evidence": evidence[-10:],
        }

    def cancelled_result() -> SubagentResult:
        return SubagentResult.cancelled(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="任务已取消，进度已保留",
            partial_result=partial(),
            l0_raw=str(archive.raw_path) if archive else None,
            l1_trace=str(archive.trace_path) if archive else None,
        )

    def failure_result(category: str, error: str) -> SubagentResult:
        return SubagentResult.failure(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="任务中断，进度已保留",
            error_category=category,
            error=error,
            partial_result=partial(),
            l0_raw=str(archive.raw_path) if archive else None,
            l1_trace=str(archive.trace_path) if archive else None,
        )

    with bind_execution_context(execution_context):
        _log_trace(archive, "run_start", {"goal": spec.goal[:200], "attempt": spec.attempt})
        for turn in range(max_turns):
            if controls.cancelled():
                return cancelled_result()
            if controls.paused():
                return PausedOutcome(checkpoint=partial())

            remaining = max_turns - turn
            if remaining <= _WRAP_UP_REMAINING and not wrapup_injected:
                wrapup_injected = True
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"[Budget notice] Only {remaining} turns remain. Stop "
                            "opening new work; write your final report from what "
                            "you have. An honest partial answer beats no answer."
                        ),
                    }
                )
            turn_tools = tools if remaining > 1 else None

            response, error = _guarded_llm_call(
                llm,
                _strip_private_keys(messages),
                turn_tools,
                controls.cancel_event.is_set,
            )
            if error == "cancelled":
                return cancelled_result()
            if error == "timeout":
                response, error = _guarded_llm_call(
                    llm,
                    _strip_private_keys(messages),
                    turn_tools,
                    controls.cancel_event.is_set,
                )
            if error == "cancelled":
                return cancelled_result()
            if error == "timeout":
                return failure_result("subagent_timeout", "LLM timed out twice")
            if error:
                _log_trace(archive, "llm_error", {"error": error})
                return failure_result("llm_error", "LLM call failed")
            if response is None:
                return failure_result("llm_error", "LLM returned empty response")

            message = response.choices[0].message

            if getattr(message, "tool_calls", None):
                messages.append(message.model_dump())
                for call_index, tool_call in enumerate(message.tool_calls):
                    tool_name = tool_call.function.name
                    args_raw = tool_call.function.arguments
                    args = _parse_args(args_raw)

                    if controls.cancelled() or controls.paused():
                        # Pair every remaining id so the transcript stays valid.
                        for pending in message.tool_calls[call_index:]:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": pending.id,
                                    "content": json.dumps(
                                        {"ok": False, "error": "task yielded"},
                                        ensure_ascii=False,
                                    ),
                                }
                            )
                        if controls.cancelled():
                            return cancelled_result()
                        return PausedOutcome(checkpoint=partial())

                    result = registry.execute(tool_name, args_raw)
                    result_dict = result.to_dict()
                    if archive is not None:
                        try:
                            archive.log_raw(tool_name, args, result_dict)
                        except TaskArchiveError:
                            logger.warning("L0 write failed for %s", spec.task_id[:8])
                    summary = _tool_summary(tool_name, args, result_dict)
                    activity.append(summary)
                    _log_trace(archive, "tool", {"summary": summary, "turn": turn + 1})
                    emit(
                        "activity",
                        {"label": summary[:160], "turn": turn + 1, "max_turns": max_turns},
                    )
                    if evidence_extractor is not None and result.ok:
                        try:
                            evidence.extend(evidence_extractor(tool_name, args, result_dict))
                        except Exception:
                            logger.exception("evidence extractor raised")

                    content = result.to_model_str()
                    if len(content) > _TOOL_RESULT_MAX_CHARS:
                        content = content[:_TOOL_RESULT_MAX_CHARS] + '…(truncated)"}'
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": content,
                        }
                    )
                    summaries[len(messages) - 1] = summary
                _age_off_tool_results(messages, summaries)
                continue

            final_text = (message.content or "").strip()
            if not final_text:
                messages.append(
                    {
                        "role": "system",
                        "content": "Empty answer is not acceptable — write the final report now.",
                    }
                )
                continue

            _log_trace(archive, "run_end", {"turns": turn + 1})
            if archive is not None:
                try:
                    archive.write_output(final_text)
                except TaskArchiveError:
                    logger.warning("output write failed for %s", spec.task_id[:8])
            return SubagentResult.completed(
                task_id=spec.task_id,
                role=spec.role,
                attempt=spec.attempt,
                summary=_headline(final_text),
                output=final_text,
                evidence=evidence[:20],
                stats={"turns_used": turn + 1, "budget": max_turns},
                l0_raw=str(archive.raw_path) if archive else None,
                l1_trace=str(archive.trace_path) if archive else None,
            )

    return failure_result("budget_exhausted", "turn budget exhausted without a final answer")


def _headline(text: str, limit: int = 300) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), text)
    first = first.lstrip("#").strip() or "任务完成"
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _read_checkpoint(archive: TaskArchive | None) -> Any:
    if archive is None:
        return None
    try:
        return archive.read_checkpoint()
    except TaskArchiveError:
        return None


def _log_trace(archive: TaskArchive | None, event: str, data: dict[str, Any]) -> None:
    if archive is None:
        return
    try:
        archive.log_trace(event, data)
    except TaskArchiveError:
        logger.warning("L1 trace write failed")
