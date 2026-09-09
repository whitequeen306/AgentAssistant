"""Researcher runner: adapts the existing deep-research loop to the manager.

The proven ``run_research_subagent`` loop (L0/L1/L2 artifacts, circuit
breaker, wrap-up mode, resume digests) stays untouched — this adapter only
injects task-scoped cancellation/progress and maps the legacy ToolResult into
the unified ``SubagentResult``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..manager import EmitFn, TaskControls
from ..models import SubagentResult, SubagentSpec

logger = logging.getLogger(__name__)

_SUMMARY_MAX_CHARS = 400


def _short_summary(text: str, fallback: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return fallback
    first_line = next((line for line in cleaned.splitlines() if line.strip()), cleaned)
    first_line = first_line.strip()
    if len(first_line) > _SUMMARY_MAX_CHARS:
        return first_line[: _SUMMARY_MAX_CHARS - 1] + "…"
    return first_line


def _evidence_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for finding in findings[:20]:
        sources = finding.get("sources") or []
        evidence.append(
            {
                "type": "web",
                "ref": sources[0] if sources else "",
                "claim": (finding.get("claim") or "")[:400],
            }
        )
    return evidence


def _artifacts_from_data(data: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for key in ("run_id", "l0_raw", "l1_trace", "l2_findings", "report_path", "resume_from"):
        value = data.get(key)
        if value:
            artifacts[key] = str(value)
    return artifacts


def researcher_runner(
    spec: SubagentSpec,
    controls: TaskControls,
    emit: EmitFn,
) -> SubagentResult:
    """Run one research task and return the unified result."""
    from agent_assistant.tools.research import run_research_subagent

    context_parts: list[str] = []
    background = str(spec.context.get("background") or "").strip()
    if background:
        context_parts.append(background)
    continue_instruction = str(spec.context.get("continue_instruction") or "").strip()
    if continue_instruction:
        context_parts.append(f"Follow-up instruction from the user: {continue_instruction}")
    resume_from = str(spec.context.get("resume_from") or "").strip() or None

    def on_progress(tool: str, label: str, turn: int, max_turns: int) -> None:
        emit(
            "progress",
            {"tool": tool, "label": label, "turn": turn, "max_turns": max_turns},
        )

    result = run_research_subagent(
        goal=spec.goal,
        context="\n\n".join(context_parts),
        resume_from=resume_from,
        cancel_check=controls.cancel_event.is_set,
        on_progress=on_progress,
    )

    data: dict[str, Any] = result.data if isinstance(result.data, dict) else {}
    findings = data.get("findings") or []
    stats = {
        "turns_used": data.get("turns_used"),
        "budget": data.get("budget"),
        "findings_count": data.get("findings_count", len(findings)),
    }
    artifacts = _artifacts_from_data(data)

    if result.ok:
        report = str(data.get("report") or "")
        narrative = str(data.get("summary") or "")
        return SubagentResult.completed(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            # Short headline only — the full report lives in output/artifacts.
            summary=_short_summary(narrative, "调研完成"),
            output=report or narrative,
            artifacts=artifacts,
            evidence=_evidence_from_findings(findings),
            next_action=_next_action_str(data),
            stats=stats,
            l0_raw=artifacts.get("l0_raw"),
            l1_trace=artifacts.get("l1_trace"),
        )

    error_category = result.error_category or "unknown"
    partial: dict[str, Any] = {
        "summary": data.get("summary"),
        "findings": findings,
        "resume_from": data.get("resume_from"),
    }
    if controls.cancel_event.is_set() or error_category == "user_cancelled":
        return SubagentResult.cancelled(
            task_id=spec.task_id,
            role=spec.role,
            attempt=spec.attempt,
            summary="调研已取消，部分进度已保留",
            partial_result=partial,
            next_action=_next_action_str(data),
            stats=stats,
            l0_raw=artifacts.get("l0_raw"),
            l1_trace=artifacts.get("l1_trace"),
        )
    return SubagentResult.failure(
        task_id=spec.task_id,
        role=spec.role,
        attempt=spec.attempt,
        summary=_short_summary(str(data.get("summary") or ""), "调研中断，部分进度已保留"),
        error_category=error_category,
        error=result.error or "research subagent failed",
        partial_result=partial,
        next_action=_next_action_str(data),
        stats=stats,
        l0_raw=artifacts.get("l0_raw"),
        l1_trace=artifacts.get("l1_trace"),
    )


def _next_action_str(data: dict[str, Any]) -> str | None:
    resume_from = data.get("resume_from")
    if not resume_from:
        return None
    return f"retry this task (progress is preserved via research run {resume_from})"
