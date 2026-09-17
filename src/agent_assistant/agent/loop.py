"""The core agentic loop.

This is the heart of the system. The model decides what tools to call;
the framework only:
  1. Sends messages + tool schemas to the LLM
  2. If the LLM returns tool_calls → executes them → appends results → loops
  3. If the LLM returns content (final answer) → returns to the caller
  4. Enforces a budget cap (max tool-call rounds) to prevent runaway

There is NO hardcoded flow orchestration here. The model reasons about
which tool, how many times, and in what order — fully agentic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from agent_assistant.agent.cancellation import bind_cancel, is_cancelled
from agent_assistant.agent.prompt import SYSTEM_PROMPT, build_system_prompt
from agent_assistant.config import settings
from agent_assistant.llm.client import llm_client
from agent_assistant.memory.manager import MemoryManager
from agent_assistant.memory.service import memory_service
from agent_assistant.memory.session_trace import (
    EVENT_LLM,
    EVENT_LLM_ERROR,
    EVENT_TOOL,
    EVENT_TURN_END,
    EVENT_TURN_START,
    EVENT_USER,
    session_trace,
)
from agent_assistant.memory.token_counter import count_messages_tokens
from agent_assistant.memory.tool_summary import summarize_tool_result
from agent_assistant.subagents.context import (
    AgentExecutionContext,
    bind_execution_context,
    current_execution_context,
)
from agent_assistant.tools.registry import tool_registry
from agent_assistant.tools.sanitize import public_llm_error, sanitize_error
from agent_assistant.tools.tool_archive import tool_archive

logger = logging.getLogger(__name__)


def _model_info() -> dict[str, str]:
    """Model identity for the event stream.

    Reads it lazily (and defensively) so a test double without a ``model``
    attribute still lets tracing work.
    """
    return {"id": getattr(llm_client, "model", None) or settings.deepseek_model}

_STOP_MESSAGE = "[已停止生成]"

# Desktop UI tools. A turn that is almost entirely these (song search, clicking
# through an app) is a stall if it hits the progress-summary budget — unlike
# research, auto-resuming just repeats inspect/click/type.
_UI_LOOP_TOOLS = frozenset({
    "ui_inspect",
    "ui_click",
    "ui_type",
    "ui_hotkey",
    "ui_scroll",
    "focus_window",
    "launch_app",
})
_UI_STALL_MIN_TOOLS = 8
_UI_STALL_RATIO = 0.7
_UI_INSPECT_STALL_LIMIT = 10

_PROGRESS_NUDGE = (
    "【进度汇报·自动】已连续执行多次工具调用。"
    "本轮禁止使用任何工具，只能用自然语言向用户简要说明："
    "已完成什么、正在等待或卡在哪一步、下一步打算怎么做。"
)
_UI_STALL_NUDGE = (
    "【进度汇报·自动】桌面操作连续多轮未见明确进展。"
    "本轮禁止使用任何工具，用自然语言告诉用户卡在哪、已经尝试了什么。"
    "这是本轮最终回复，不要假设系统还会自动继续点选。"
)

# Hosted mode (LianYu MCP bridge): the loop's content deltas are forwarded to
# the chat as live progress bubbles, so the model can narrate milestones —
# sparingly; templated status lines cover the gaps between them.
_HOSTED_NARRATION_SECTION = """\
# 现场解说（托管模式）
你正被恋语角色委托，在用户电脑上后台执行任务。任务期间你写出的每句 content 都会作为进度气泡实时展示给用户（不影响工具调用与最终回复）。
- 只在「阶段切换 / 有新进展 / 受阻或失败」时，用 content 写一句简短解说（≤25字），如「先打开网易云音乐」「找到这首歌了，点播放」。
- 连续的微操作（看界面、点控件、输字）不要每步都解说——话说多了反而吵，保持安静把活干完。
- 解说要口语、面向用户；不提工具名、控件 id、文件路径；不要 emoji。
- 最终回复照旧：任务结果 + 用户需要知道的信息。
"""


def _ui_turn_is_desktop(tool_names: list[str]) -> bool:
    """True when this turn's tool calls have been mostly desktop UI ops."""
    if not tool_names:
        return False
    ui = sum(1 for name in tool_names if name in _UI_LOOP_TOOLS)
    return (ui / len(tool_names)) >= _UI_STALL_RATIO


def _ui_loop_stalled(tool_names: list[str]) -> bool:
    """True when a UI turn has gone long enough to treat as a stall."""
    if len(tool_names) < _UI_STALL_MIN_TOOLS:
        return False
    return _ui_turn_is_desktop(tool_names)


def _should_force_ui_stop(tool_names: list[str]) -> bool:
    """Stop early when inspect is looping (NetEase-style search ping-pong)."""
    if not _ui_loop_stalled(tool_names):
        return False
    inspects = sum(1 for name in tool_names if name == "ui_inspect")
    return inspects >= _UI_INSPECT_STALL_LIMIT


@dataclass
class AgentEvent:
    """An event emitted during the agent loop for UI/CLI rendering."""

    type: str  # "thinking"|"tool_call"|"tool_result"|"response_chunk"|"response"|"error"
    content: Any = None
    conversation_id: str | None = None
    parent_turn_id: str | None = None


class AgentLoop:
    """Runs the agentic conversation loop.

    Maintains conversation history and processes user messages through
    the LLM + tool execution cycle.
    """

    def __init__(
        self,
        system_prompt: str = SYSTEM_PROMPT,
        max_rounds: int | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        memory_manager: MemoryManager | None = None,
        conversation_id: str = "local",
    ) -> None:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        self._conversation_id = conversation_id
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]
        self._max_rounds = max_rounds or settings.max_tool_rounds
        self._progress_summary_every = max(1, int(settings.progress_summary_every or 20))
        # One-shot flag so the nudge is injected exactly once per threshold hit.
        self._pending_summary_nudge = False
        self._on_event = on_event or (lambda e: None)
        # Per-loop short-term window — must NOT be the process-global manager,
        # or conversation A’s compaction summary bleeds into conversation B.
        self._memory_manager = memory_manager or MemoryManager(
            token_budget=settings.effective_memory_budget,
            summary_cap=settings.memory_summary_cap,
        )
        # Compaction/pruning events need to land in THIS session's event
        # stream. Attached even for an injected manager: the trace is
        # redirected to a temp dir under pytest (see tests/conftest.py).
        self._memory_manager.attach_trace(session_trace, self._conversation_id)
        # User-turn counter for the event stream (turn_start / turn_end).
        self._turn_no = 0
        # Serializes chat/load_history/reset on THIS loop (e.g. double-send on
        # the same conversation). Cross-conversation isolation is AgentPool's job.
        self._lock = threading.RLock()
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        """Cooperatively stop the in-flight turn (stream / tool rounds)."""
        self._cancel.set()

    @property
    def conversation_id(self) -> str:
        """Stable identity of the conversation owned by this loop."""
        return self._conversation_id

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Current conversation history (read-only view)."""
        with self._lock:
            return list(self._messages)

    def reset(self) -> None:
        """Clear conversation history, keep system prompt."""
        with self._lock:
            self._messages = [self._messages[0]]

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        """Replace history with stored turns (UI conversation switching).

        Keeps the system prompt; only user/assistant text turns are
        restored (tool traces are not persisted).
        """
        with self._lock:
            self._messages = [self._messages[0]] + [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]

    def chat(self, user_message: str, *, turn_id: str | None = None) -> str:
        """Synchronous wrapper for the async chat loop.

        Convenience for callers that don't have an event loop
        (CLI, tests). Internally runs the async version.

        Threading invariant: ``asyncio.run`` runs the coroutine on the CALLING
        thread, so every ``self._on_event(...)`` inside ``achat`` fires on this
        same thread (inline; the one ``asyncio.to_thread`` tool call resumes back
        here before its ``tool_result`` event). ``ui/bridge.py``'s background
        report suppress (``_report_tls.silent`` / ``report_silent``) RELIES on
        this — it sets the thread-local in the report thread so on_event (same
        thread) skips streaming to the current view. If a future refactor moves
        ``_on_event`` to a worker thread (e.g. ``to_thread``), that suppress
        breaks silently. Don't.
        """
        # Hold the lock across the whole turn so a second send on the same
        # conversation cannot interleave into _messages mid tool-call.
        with self._lock:
            return asyncio.run(self.achat(user_message, turn_id=turn_id))

    async def achat(
        self,
        user_message: str,
        *,
        turn_id: str | None = None,
    ) -> str:
        """Process a user message through the full agentic loop (async).

        Returns the final text response from the model.
        Emits events via on_event callback for intermediate steps.

        Callers that enter via ``chat()`` already hold ``_lock``. Direct
        ``achat()`` callers (tests) must not race another turn on the same
        instance — take the lock yourself if sharing the loop across threads.
        """
        parent_turn_id = turn_id or uuid.uuid4().hex
        context = AgentExecutionContext(
            conversation_id=self._conversation_id,
            parent_turn_id=parent_turn_id,
            owner_id=(
                f"main:{self._conversation_id}:{parent_turn_id}:"
                f"{uuid.uuid4().hex}"
            ),
            cancel_event=self._cancel,
        )
        self._cancel.clear()
        # Event-stream boundary for this user turn. turn_start/turn_end bracket
        # everything below, so an auditor can slice events by turn.
        self._turn_no += 1
        turn = self._turn_no
        session_trace.event(
            self._conversation_id, EVENT_TURN_START, turn=turn,
            summary=user_message,
        )
        with bind_execution_context(context), bind_cancel(self._cancel):
            # Refresh system prompt with memory context (profile + rolling summary)
            self._refresh_system_prompt()

            # Append user message
            self._messages.append({"role": "user", "content": user_message})
            session_trace.event(
                self._conversation_id, EVENT_USER, turn=turn,
                summary=user_message,
                chars=len(user_message),
            )

            try:
                reply = await self._run_rounds()
            except Exception as exc:
                session_trace.event(
                    self._conversation_id, EVENT_TURN_END, turn=turn, ok=False,
                    error_category=type(exc).__name__,
                    summary=f"回合异常结束：{type(exc).__name__}",
                )
                raise
            session_trace.event(
                self._conversation_id, EVENT_TURN_END, turn=turn,
                summary=f"回合结束，回复 {len(reply or '')} 字符",
                chars=len(reply or ""),
            )
            return reply

    def _emit_event(self, event_type: str, content: Any = None) -> None:
        """Emit an event attributed to the currently bound execution turn."""
        context = current_execution_context()
        self._on_event(
            AgentEvent(
                type=event_type,
                content=content,
                conversation_id=(
                    context.conversation_id if context is not None else None
                ),
                parent_turn_id=(
                    context.parent_turn_id if context is not None else None
                ),
            )
        )

    async def _finish_cancelled(self, partial: str = "") -> str:
        content = (
            (partial.strip() + "\n\n" + _STOP_MESSAGE).strip()
            if partial.strip()
            else _STOP_MESSAGE
        )
        self._messages.append({"role": "assistant", "content": content})
        self._emit_event("response", content)
        return content

    async def _run_rounds(self) -> str:
        """Agentic loop body (runs under bind_cancel)."""
        # Compact failure rows for this user turn — appended into each failed
        # tool message so the model sees a short strategy hint + recent trace.
        from agent_assistant.tools.failure_feedback import make_failure_entry

        turn_failures: list[dict] = []
        # Narration emitted in tool rounds (「让我看看…」 commentary) — the
        # final response event carries narration + final answer so the UI
        # bubble's accumulated content survives finalize.
        turn_narrations: list[str] = []
        # Tool rounds since the last forced progress summary (this turn).
        # Counts ACTUAL tool rounds (model returned tool_calls), not total API
        # calls — so the summary round itself doesn't consume the budget.
        tool_rounds_since_summary = 0
        turn_tool_names: list[str] = []
        force_text_round = False
        self._pending_summary_nudge = False

        # Agentic loop: model decides tools until it produces a final answer
        for round_idx in range(self._max_rounds):
            if is_cancelled():
                return await self._finish_cancelled()

            logger.debug("Agent loop round %d", round_idx + 1)

            # A4: Compact messages if over token budget. If compaction dropped
            # anything, refresh the system prompt immediately so the rolling
            # summary is visible in THIS turn's next API call — otherwise the
            # dropped task context only reappears on the next user turn.
            # Dropping is only allowed on the turn's FIRST model call: from
            # then on the model is mid-task, and removing the messages that
            # produced its current state makes it lose the thread (it still
            # sees the latest tool results, but not why it ran them). Later
            # rounds shrink payloads only and defer dropping to the next turn.
            dropped_before = self._memory_manager.dropped_count
            self._messages = self._compact_messages(allow_dropping=round_idx == 0)
            if self._memory_manager.dropped_count != dropped_before:
                self._refresh_system_prompt()

            # Heal any orphaned assistant(tool_calls) before the API call —
            # leftover from a prior crash/race/compaction. Logging alone still
            # 400s; repair inserts synthetic tool failures so the turn can proceed.
            self._repair_orphaned_tool_calls()

            # Diagnostic: validate + log the message shape before the API
            # call. Catches an orphaned assistant(tool_calls) (missing tool
            # message) at the point it would 400 — see data_dir/logs/agent.log.
            self._log_pre_api(round_idx)

            # Emit thinking event at start of each round
            self._emit_event("thinking")

            # Progress-summary round: no tools advertised so the model MUST
            # answer with text (stops infinite tool ping-pong at the budget
            # threshold without aborting the whole conversation).
            summary_due = (
                force_text_round
                or tool_rounds_since_summary >= self._progress_summary_every
            )
            tools = [] if summary_due else tool_registry.openai_schemas()
            try:
                stream = await llm_client.achat_stream(
                    messages=self._messages,
                    tools=tools if tools else None,
                )
            except Exception as exc:
                logger.exception(
                    "LLM achat_stream failed (round %d): %d messages, "
                    "orphans=%s",
                    round_idx + 1,
                    len(self._messages),
                    self._find_orphaned_tool_calls(),
                )
                session_trace.event(
                    self._conversation_id, EVENT_LLM_ERROR, turn=self._turn_no,
                    ok=False,
                    error_category=type(exc).__name__,
                    summary=f"模型调用失败：{type(exc).__name__}",
                    model=_model_info(),
                    round=round_idx + 1,
                )
                raise RuntimeError(public_llm_error(exc)) from exc

            # Accumulate streamed response
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_acc: dict[int, dict[str, Any]] = {}  # index → {id, name, args}
            cancelled_mid_stream = False

            async for chunk in stream:
                if is_cancelled():
                    cancelled_mid_stream = True
                    break

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                # DeepSeek thinking mode: CoT before final content / tool_calls
                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    self._emit_event("thinking_chunk", reasoning_delta)

                # Content delta → forward to UI as response_chunk
                if delta.content:
                    content_parts.append(delta.content)
                    self._emit_event("response_chunk", delta.content)

                # Tool call deltas → accumulate
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {
                                "id": "", "name": "", "arguments": ""
                            }
                        if tc_delta.id:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls_acc[idx]["name"] += (
                                    tc_delta.function.name
                                )
                            if tc_delta.function.arguments:
                                tool_calls_acc[idx]["arguments"] += (
                                    tc_delta.function.arguments
                                )

            if cancelled_mid_stream:
                # Incomplete tool_calls must not be persisted — orphan risk.
                return await self._finish_cancelled("".join(content_parts))

            reasoning_text = "".join(reasoning_parts)

            # Case 1: Model returned tool calls → execute and loop
            if tool_calls_acc:
                # Build assistant message with tool_calls
                tc_list = []
                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    # Some OpenAI-compatible providers stream tool_calls without
                    # an id; an empty id makes the API reject the next request
                    # with 400 "assistant with tool_calls must be followed by
                    # tool messages" (no id to correlate). Fill a fallback.
                    tc_id = tc["id"] or f"call_{uuid.uuid4().hex[:24]}"
                    tc_list.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    })
                # DeepSeek + tools: reasoning_content MUST be echoed back or
                # the next request 400s. Keep any interim content too.
                assistant_tc: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": tc_list,
                }
                if reasoning_text:
                    assistant_tc["reasoning_content"] = reasoning_text
                self._messages.append(assistant_tc)
                if content_parts:
                    turn_narrations.append("".join(content_parts))

                session_trace.event(
                    self._conversation_id, EVENT_LLM, turn=self._turn_no,
                    round=round_idx + 1,
                    summary=(
                        f"第 {round_idx + 1} 轮：决定调用 "
                        f"{', '.join(tc['function']['name'] for tc in tc_list)}"
                    ),
                    tool_calls=[tc["function"]["name"] for tc in tc_list],
                    model=_model_info(),
                )

                for ti, tc in enumerate(tc_list):
                    if is_cancelled():
                        # Still must pair every remaining tool_call_id.
                        for pending in tc_list[ti:]:
                            self._messages.append({
                                "role": "tool",
                                "tool_call_id": pending["id"],
                                "content": json.dumps(
                                    {"ok": False, "error": "cancelled"},
                                    ensure_ascii=False,
                                ),
                            })
                        return await self._finish_cancelled()

                    fn_name = tc["function"]["name"]
                    fn_args = tc["function"]["arguments"]
                    tc_id = tc["id"]
                    turn_tool_names.append(fn_name)

                    # Notify UI of the call (best-effort). A handler bug here
                    # must NOT block tool execution or skip the tool message —
                    # that would re-orphan the assistant(tool_calls) message.
                    try:
                        self._emit_event(
                            "tool_call",
                            {"name": fn_name, "arguments": fn_args},
                        )
                    except Exception:
                        logger.exception(
                            "on_event(tool_call) handler raised for '%s'",
                            fn_name,
                        )

                    # Execute + serialize for the model. ANY failure here
                    # (tool raising despite the registry's no-raise contract,
                    # a tool returning non-JSON-serializable data — Path,
                    # datetime, set, bytes — making to_model_str()/json.dumps
                    # raise) MUST still produce a tool message for this
                    # tool_call_id. Otherwise the assistant(tool_calls) message
                    # is orphaned and, since _messages persists across turns,
                    # EVERY later API call 400s with "insufficient tool messages
                    # following tool_calls message".
                    result_dict: dict[str, Any]
                    tool_content: str
                    try:
                        result = await asyncio.to_thread(
                            tool_registry.execute, fn_name, fn_args
                        )
                        result_dict = result.to_dict()
                        if not result.ok:
                            turn_failures.append(
                                make_failure_entry(
                                    fn_name,
                                    error=result.error,
                                    code=result.code,
                                    error_category=result.error_category,
                                    arguments=fn_args,
                                )
                            )
                            tool_content = result.to_model_str(
                                turn_trace=turn_failures
                            )
                            # Keep UI/event dict in sync with what the model saw.
                            result_dict = json.loads(tool_content)
                        else:
                            tool_content = result.to_model_str()
                    except Exception as exc:
                        logger.exception(
                            "Tool '%s' execution/serialization failed; "
                            "emitting failure tool message to keep the "
                            "tool_calls pairing intact",
                            fn_name,
                        )
                        # B9: the model sees only a safe category message,
                        # never raw exception detail (paths/creds/versions).
                        safe = sanitize_error(
                            str(exc), context=fn_name, code=500
                        )
                        from agent_assistant.tools.failure_feedback import (
                            build_failure_payload,
                        )

                        turn_failures.append(
                            make_failure_entry(
                                fn_name,
                                error=safe.safe_message,
                                code=500,
                                error_category=safe.category or "unknown",
                                arguments=fn_args,
                            )
                        )
                        result_dict = build_failure_payload(
                            error=safe.safe_message,
                            code=500,
                            error_category=safe.category or "unknown",
                            turn_trace=turn_failures,
                        )
                        tool_content = json.dumps(
                            result_dict, ensure_ascii=False
                        )

                    # Append the tool message FIRST — this invariant-critical
                    # line must not be skippable by a UI handler crash (a
                    # raise here would re-orphan the tool_calls message).
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_content,
                    })

                    # Persist the FULL call + result for later recall
                    # (compaction/stubbing shrink context; the archive keeps
                    # the originals retrievable via recall_tool_result).
                    # record() never raises; guard anyway so archiving can
                    # never break the turn.
                    detail_ref: str | None = None
                    try:
                        detail_ref = tool_archive.record(
                            conversation_id=self._conversation_id,
                            call_id=tc_id,
                            tool=fn_name,
                            arguments=fn_args,
                            result=result_dict,
                        )
                    except Exception:
                        logger.warning(
                            "tool_archive.record raised for '%s'", fn_name,
                            exc_info=True,
                        )

                    # Event stream: one line per call carrying the SAME
                    # one-line summary the model sees in context, plus ok /
                    # failure category and a pointer into tool_details.jsonl.
                    session_trace.event(
                        self._conversation_id, EVENT_TOOL, turn=self._turn_no,
                        round=round_idx + 1,
                        ok=bool(result_dict.get("ok")),
                        error_category=result_dict.get("error_category"),
                        summary=summarize_tool_result(
                            fn_name, arguments=fn_args, result_json=result_dict
                        ),
                        detail_ref=detail_ref,
                        tool=fn_name,
                        call_id=tc_id,
                    )

                    # Notify UI of the result (best-effort, AFTER the pairing
                    # invariant is satisfied so a handler crash can't break
                    # it).
                    try:
                        self._emit_event(
                            "tool_result",
                            {"name": fn_name, "result": result_dict},
                        )
                    except Exception:
                        logger.exception(
                            "on_event(tool_result) handler raised for '%s'",
                            fn_name,
                        )

                    # Cancel requested during a long tool — pair the rest, stop.
                    if is_cancelled() and ti + 1 < len(tc_list):
                        for pending in tc_list[ti + 1:]:
                            self._messages.append({
                                "role": "tool",
                                "tool_call_id": pending["id"],
                                "content": json.dumps(
                                    {"ok": False, "error": "cancelled"},
                                    ensure_ascii=False,
                                ),
                            })
                        return await self._finish_cancelled()

                # Continue the loop — model sees results and decides next
                if is_cancelled():
                    return await self._finish_cancelled()

                # Progress summary gate: every N tool rounds, inject a system
                # nudge so the NEXT model call sees no tools and must answer
                # with text (the "summary_due" flag at the top of the loop).
                tool_rounds_since_summary += 1
                ui_desktop = _ui_turn_is_desktop(turn_tool_names)
                force_ui_stop = _should_force_ui_stop(turn_tool_names)
                hit_round_budget = (
                    tool_rounds_since_summary >= self._progress_summary_every
                )
                if (
                    (hit_round_budget or force_ui_stop)
                    and round_idx + 1 < self._max_rounds
                    and not self._pending_summary_nudge
                ):
                    self._pending_summary_nudge = True
                    if force_ui_stop:
                        force_text_round = True
                    self._messages.append({
                        "role": "system",
                        "content": (
                            _UI_STALL_NUDGE if ui_desktop else _PROGRESS_NUDGE
                        ),
                    })
                continue

            # Case 2: Model returned final content (no tool calls)
            content = "".join(content_parts)
            # Full turn text = narration from earlier tool rounds + final
            # answer. The UI bubble accumulated all rounds' deltas live; the
            # final event must carry the same full text or finalize would
            # wipe the intermediate narration ("让我看看…" commentary).
            full_content = content if not turn_narrations else "".join([*turn_narrations, content])
            assistant_final: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            # Optional for non-tool turns (API ignores on next user turn),
            # but keep if this conversation already mixes tool rounds.
            if reasoning_text:
                assistant_final["reasoning_content"] = reasoning_text
            self._messages.append(assistant_final)

            self._emit_event("response", full_content)
            session_trace.event(
                self._conversation_id, EVENT_LLM, turn=self._turn_no,
                round=round_idx + 1,
                summary=full_content,
                final_answer=True,
                tokens={"window": count_messages_tokens(self._messages)},
                model=_model_info(),
            )

            if summary_due:
                # Forced text-only round. Research turns resume with tools;
                # a UI-dominated stall (inspect/click/type ping-pong) stops
                # here so we don't spend another 10 rounds repeating the
                # same search.
                tool_rounds_since_summary = 0
                self._pending_summary_nudge = False
                force_text_round = False
                # The report is no longer registered as a compaction
                # checkpoint: compaction now *concatenates* summary layers
                # (older layers are never re-summarized), which removes the
                # detail-loss failure mode that checkpoints existed to prevent.
                # The nudge did its job (the forced text round happened);
                # keeping it in history would waste context every round.
                self._remove_progress_nudges()
                if _ui_turn_is_desktop(turn_tool_names):
                    logger.info(
                        "UI tool loop stalled after %d tool calls; "
                        "ending turn at progress summary",
                        len(turn_tool_names),
                    )
                    return full_content
                continue
            return full_content

        # Budget exhausted (safety net; should be rare with progress summaries)
        fallback = (
            "这次会话内工具轮次已达安全上限，我先停在这里。"
            "你可以把刚才的进度总结贴给我，或换个说法让我继续。"
        )
        self._messages.append({"role": "assistant", "content": fallback})
        self._emit_event("error", fallback)
        return fallback

    def _remove_progress_nudges(self) -> None:
        """Drop consumed progress-summary nudges from history.

        The forced text round already happened — the nudge (a system message
        injected right before it) has no further effect and would otherwise
        ride along in every later API call until the next compaction.
        Exact-content match on the two nudge constants; never touches the
        main system prompt (messages[0]).
        """
        before = len(self._messages)
        self._messages = [
            m
            for m in self._messages
            if not (
                m.get("role") == "system"
                and m.get("content") in (_PROGRESS_NUDGE, _UI_STALL_NUDGE)
            )
        ]
        if len(self._messages) != before:
            logger.info(
                "Removed %d consumed progress nudge(s)",
                before - len(self._messages),
            )

    def _refresh_system_prompt(self) -> None:
        """Update system prompt with profile (shared) + this loop's summary."""
        base_prompt = build_system_prompt("full")
        if getattr(settings, "hosted_narration", False):
            base_prompt = base_prompt.rstrip() + "\n\n" + _HOSTED_NARRATION_SECTION.strip() + "\n"
        additions = memory_service.build_profile_additions()
        study_profile = memory_service.build_study_profile_section()
        if study_profile:
            additions += study_profile
        summary = self._memory_manager.build_context_prefix()
        full_prompt = base_prompt + additions + summary
        self._messages[0] = {"role": "system", "content": full_prompt}

    def _compact_messages(self, *, allow_dropping: bool = True) -> list[dict[str, Any]]:
        """Run this conversation's memory compaction if over token budget.

        ``allow_dropping=False`` shrinks old payloads but never removes a
        message — see ``MemoryManager.maybe_compact``.
        """
        return self._memory_manager.maybe_compact(
            self._messages, allow_dropping=allow_dropping
        )

    def _find_orphaned_tool_calls(self) -> list[dict[str, Any]]:
        """Detect assistant(tool_calls) ids lacking an immediate tool reply.

        OpenAI requires tool messages to *immediately* follow the assistant
        tool_calls message. A tool_call_id that only appears later (after a
        user/assistant turn) still 400s — those count as orphans here too.
        Returns one entry per orphan: {msg_index, tool_call_id, tool_name}.
        """
        orphans: list[dict[str, Any]] = []
        msgs = self._messages
        for i, m in enumerate(msgs):
            if m.get("role") != "assistant" or not m.get("tool_calls"):
                continue
            # Contiguous tool block right after this assistant message.
            seen: set[str] = set()
            j = i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                tid = msgs[j].get("tool_call_id")
                if tid:
                    seen.add(tid)
                j += 1
            for tc in m["tool_calls"]:
                tc_id = tc.get("id")
                if tc_id and tc_id not in seen:
                    orphans.append({
                        "msg_index": i,
                        "tool_call_id": tc_id,
                        "tool_name": tc.get("function", {}).get("name", "?"),
                    })
        return orphans

    def _repair_orphaned_tool_calls(self) -> int:
        """Insert synthetic tool failures for any unpaired tool_call_ids.

        Mutates ``_messages`` in place so the next API call is well-formed.
        Returns the number of synthetic tool messages inserted.
        """
        orphans = self._find_orphaned_tool_calls()
        if not orphans:
            return 0

        # Group by assistant message index (descending insert to keep indices stable
        # within a single assistant is unnecessary if we rebuild via walk).
        by_index: dict[int, list[dict[str, Any]]] = {}
        for o in orphans:
            by_index.setdefault(o["msg_index"], []).append(o)

        repaired = 0
        # Walk ascending; insert right after each assistant's existing tool block.
        for msg_index in sorted(by_index.keys()):
            # Re-find insert point each time (prior inserts shift later indices).
            # Locate the assistant message by tool_call_id of the first orphan.
            target_ids = {o["tool_call_id"] for o in by_index[msg_index]}
            asst_i = None
            for i, m in enumerate(self._messages):
                if m.get("role") != "assistant" or not m.get("tool_calls"):
                    continue
                ids = {tc.get("id") for tc in m["tool_calls"]}
                if target_ids & ids:
                    asst_i = i
                    break
            if asst_i is None:
                continue

            j = asst_i + 1
            while j < len(self._messages) and self._messages[j].get("role") == "tool":
                j += 1

            present = {
                self._messages[k].get("tool_call_id")
                for k in range(asst_i + 1, j)
            }
            for o in by_index[msg_index]:
                tc_id = o["tool_call_id"]
                if tc_id in present:
                    continue
                name = o.get("tool_name") or "unknown"
                synthetic = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": (
                                f"tool result missing for '{name}' "
                                "(auto-repaired unpaired tool_calls)"
                            ),
                            "code": 500,
                        },
                        ensure_ascii=False,
                    ),
                }
                self._messages.insert(j, synthetic)
                j += 1
                present.add(tc_id)
                repaired += 1

        if repaired:
            logger.warning(
                "Repaired %d orphaned tool_call_id(s) with synthetic tool "
                "failures so the API call can proceed: %s",
                repaired,
                orphans,
            )
        return repaired

    def _log_pre_api(self, round_idx: int) -> None:
        """Validate + log the message list before each LLM API call.

        Surfaces the exact 'insufficient tool messages' invariant break at
        the point it would 400, so the remaining orphan source is visible
        in the run log (data_dir/logs/agent.log).
        """

        def _tag(m: dict[str, Any]) -> str:
            r = m.get("role", "?")
            if r == "assistant" and m.get("tool_calls"):
                return f"assistant(tool_calls:{len(m['tool_calls'])})"
            if r == "tool":
                tcid = str(m.get("tool_call_id", "?"))
                return f"tool({tcid[:12]})"
            return r

        logger.info(
            "round %d/%d → API call: %d messages, roles=%s",
            round_idx + 1, self._max_rounds,
            len(self._messages), [_tag(m) for m in self._messages],
        )
        orphans = self._find_orphaned_tool_calls()
        if orphans:
            logger.error(
                "INVARIANT BROKEN before API call (after repair attempt): %d "
                "assistant(tool_calls) tool_call_id(s) still unpaired → OpenAI "
                "will 400 'insufficient tool messages following tool_calls "
                "message'. orphans=%s",
                len(orphans), orphans,
            )
