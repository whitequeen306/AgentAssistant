"""Memory manager — short-term window + rolling summary + compaction.

Core logic:
1. Track token count of conversation messages
2. When over budget: summarize oldest messages → rolling summary → drop them
3. Keep message pairs complete (never split user/assistant)
4. Rolling summary has its own cap; when exceeded, re-summarize (compaction)

The summarizer is injectable for testing. Default uses the LLM client.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from agent_assistant.memory.session_trace import (
    EVENT_COMPACTION,
    EVENT_PRUNING,
    EVENT_SUMMARY_ARCHIVED,
)
from agent_assistant.memory.summary_archive import (
    archive_layers,
    pointer_line,
    split_layers,
)
from agent_assistant.memory.token_counter import count_messages_tokens, count_tokens
from agent_assistant.memory.tool_summary import argument_hint, summarize_tool_result

logger = logging.getLogger(__name__)

# Type for summarizer function: takes text, returns summary
Summarizer = Callable[[str], str]


@dataclass(frozen=True)
class SnapshotSpec:
    """Snapshot semantics declared by a tool (see ``Tool.is_snapshot``)."""

    key_fn: Callable[[str], str]  # raw args → supersede scope key
    keep_recent: int | None = None  # keep N newest non-empty per tool; None = supersession only


def _registry_snapshot_resolver(tool_name: str) -> SnapshotSpec | None:
    """Default resolver: read snapshot semantics from the tool registry.

    The manager knows no tool names — each tool declares (via ``Tool``)
    whether its results are supersede-able snapshots and how to derive the
    supersede key. Returns None for unknown / non-snapshot tools.
    """
    from agent_assistant.tools.registry import tool_registry

    tool = tool_registry.get(tool_name)
    if tool is None or not getattr(tool, "is_snapshot", False):
        return None
    keep = getattr(tool, "snapshot_keep_recent", None)
    return SnapshotSpec(key_fn=tool.snapshot_key, keep_recent=keep)


def _default_summarizer(text: str) -> str:
    """Default summarizer using the LLM client (sync)."""
    from agent_assistant.llm.client import llm_client

    response = llm_client.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a conversation summarizer for an agentic desktop "
                    "assistant. Condense the excerpt into a short summary with "
                    "these section headings: '## 目标' — the user's CURRENT "
                    "request or goal (always present); '## 已完成' — steps "
                    "already done (tool names + key arguments + outcomes); "
                    "'## 待办/阻塞' — what is still pending or blocked, and "
                    "why; '## 关键事实' — other key facts, decisions, and file "
                    "paths. Skip a section entirely when it has no content — "
                    "never write \"无\" or \"none\". Keep file paths EXACTLY as "
                    "they appeared. The assistant will resume the task from "
                    "your summary alone — never omit the goal or the pending "
                    "state. Output only the summary."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content or ""


class MemoryManager:
    """Manages the short-term conversation window with rolling summary.

    Parameters:
        token_budget: max tokens for the message window (excluding system prompt)
        summary_cap: max tokens for the rolling summary
        summarizer: callable that summarizes text (injectable for tests)
    """

    def __init__(
        self,
        token_budget: int = 12000,
        summary_cap: int = 1200,
        summarizer: Summarizer | None = None,
        snapshot_resolver: Callable[[str], SnapshotSpec | None] | None = None,
    ) -> None:
        self._token_budget = token_budget
        self._summary_cap = summary_cap
        self._summarizer = summarizer or _default_summarizer
        self._snapshot_resolver = snapshot_resolver or _registry_snapshot_resolver

        self._rolling_summary: str = ""
        self._dropped_count: int = 0
        # Optional session trace (events.jsonl). Left unset by default so a
        # bare MemoryManager — e.g. in tests — never writes to disk; the
        # agent loop attaches the real one in __init__.
        self._trace: Any = None
        self._session_id: str = ""

    def attach_trace(self, trace: Any, session_id: str) -> None:
        """Record compaction/pruning events to the session's event stream.

        Optional on purpose: without it the manager behaves exactly as
        before (no observer, no I/O).
        """
        self._trace = trace
        self._session_id = session_id

    def _emit_trace(self, event: str, **fields: Any) -> None:
        """Best-effort event write — tracing must never break a turn."""
        if self._trace is None:
            return
        try:
            self._trace.event(self._session_id, event, **fields)
        except Exception:  # pragma: no cover — the writer swallows its own
            logger.debug("session trace event failed (%s)", event, exc_info=True)

    @property
    def rolling_summary(self) -> str:
        """Current rolling summary of dropped conversation."""
        return self._rolling_summary

    @property
    def dropped_count(self) -> int:
        """Number of messages dropped so far."""
        return self._dropped_count

    def maybe_compact(
        self,
        messages: list[dict[str, Any]],
        *,
        allow_dropping: bool = True,
    ) -> list[dict[str, Any]]:
        """Check token budget and compact if needed.

        Returns the (possibly truncated) message list.
        Never modifies the input list in place.

        Rules:
        - messages[0] (system prompt) is NEVER dropped
        - Keeps the most recent messages that fit in budget
        - The newest user message (the in-flight task) is always kept
        - Dropped messages are summarized into rolling_summary first
        - Message pairs (user→assistant) are kept complete

        ``allow_dropping=False`` runs the lossless passes only (snapshot
        stubbing + argument/result shrinking) and removes nothing. The loop
        passes that while a user turn is still in flight: shrinking usually
        brings the window back under budget anyway, whereas dropping messages
        mid-turn makes the model lose the beginning of the work it is doing —
        it keeps seeing the last tool rounds while the steps that produced
        them are gone. Dropping is deferred to the next user turn.
        """
        if not messages:
            return messages

        # Pass 0: stub superseded snapshot payloads (cheap, runs every round,
        # even under budget). A second ui_inspect of the same window makes the
        # first tree dead weight — replacing it with a stub often brings the
        # conversation back under budget WITHOUT dropping anything.
        messages = self._stub_superseded_snapshots(messages)
        # Own shallow copy first: aging replaces list slots in place, and
        # when stubbing found nothing the list above still aliases the
        # caller's list (maybe_compact promises not to mutate its input).
        messages = list(messages)
        # Pass 0b: Pruning — degrade whole tool-call groups that fell outside
        # the token protect line. Each degraded group collapses to TWO
        # one-liners (a result summary + an argument placeholder), never a
        # half-read fragment. Runs every round and is idempotent; the
        # untouched originals live in tool_details.jsonl.
        aged_results, aged_args = self._age_off_old_groups(messages)
        if aged_args or aged_results:
            # Audited so "why is this a one-liner now?" is answerable after
            # the fact — the original text lives in tool_details.jsonl.
            self._emit_trace(
                EVENT_PRUNING,
                summary=(
                    f"pruning：{aged_results} 条工具结果改为一句话摘要，"
                    f"{aged_args} 组旧参数改为占位（原文见 tool_details）"
                ),
                results_summarized=aged_results,
                args_shrunk=aged_args,
            )

        system_msg = messages[0]
        conversation = messages[1:]  # everything after system prompt

        # Count tokens for conversation part only
        conv_tokens = count_messages_tokens(conversation)

        if (
            not allow_dropping
            and conv_tokens <= self._token_budget * self._MIDTURN_HARD_RATIO
        ):
            # Mid-turn: the lossless passes above already ran. Stop here — the
            # alternative is the model losing its own earlier steps.
            #
            # The ratio is the escape hatch: if one turn has grown far past
            # budget, refusing to drop forever means the next API call
            # overflows the model, which is strictly worse than a summarised
            # middle. Beyond it we drop like any other round.
            return messages

        # Under budget → no compaction needed
        if conv_tokens <= self._token_budget:
            return list(messages)

        # Tiny overshoots used to fire a 5–7s summarizer every other UI round
        # (6195 > 6000, then 6078 > 6000…). Wait until we're clearly over.
        excess = conv_tokens - self._token_budget
        margin = max(400, self._token_budget // 10)
        if excess <= margin:
            logger.debug(
                "over budget by %d tokens (margin %d); skip compaction",
                excess,
                margin,
            )
            return list(messages)

        logger.info(
            "Memory compaction triggered: %d tokens > %d budget",
            conv_tokens,
            self._token_budget,
        )

        # Find the split point: keep recent messages within budget
        keep_from = self._find_split_point(conversation)

        if keep_from >= len(conversation):
            # Aligned split walked off the end (a single oversized tool group).
            # Walk BACK to the start of the last complete group instead of
            # keeping nothing — the model must see its own latest result once.
            j = len(conversation) - 1
            while j > 0 and conversation[j].get("role") == "tool":
                j -= 1
            keep_from = j

        # Anchor: the in-flight turn's user instruction. Dropping it wipes the
        # task (the model then sees a bare system prompt and answers with an
        # idle greeting) — task amnesia is unrecoverable, so the anchor always
        # survives. But the REST of an agentic turn may be compacted: hosted
        # computer_task turns have ONE user message + dozens of tool rounds,
        # and refusing to drop any of it let history grow unbounded
        # (126K tokens/round observed). Summarize the middle, keep the anchor
        # plus the recent window.
        last_user_idx = 0
        for i in range(len(conversation) - 1, -1, -1):
            if conversation[i].get("role") == "user":
                last_user_idx = i
                break

        if keep_from <= 0:
            # Nothing to drop (everything fits in the keep window)
            return list(messages)

        if keep_from > last_user_idx:
            anchor = conversation[last_user_idx]
            to_drop = (
                conversation[:last_user_idx]
                + conversation[last_user_idx + 1 : keep_from]
            )
            to_keep = [anchor] + conversation[keep_from:]
        else:
            to_drop = conversation[:keep_from]
            to_keep = conversation[keep_from:]

        # Drop trailing incomplete tool-call groups from the keep window:
        # the cut is by token, so a bare assistant(tool_calls) whose result has
        # not been appended yet (e.g. a concurrent turn) can land in to_keep
        # and 400 the next API call.
        to_keep, stripped = self._strip_incomplete_tool_groups(to_keep)
        if stripped:
            to_drop = to_drop + stripped

        if not to_drop:
            return list(messages)

        # Absorb dropped messages into the rolling summary as a NEW layer
        # (concatenated after the existing ones — older layers are never
        # re-summarized). A summarizer failure (network blip) must not kill
        # the in-flight turn — skip compaction this round and retry next.
        try:
            self._absorb_dropped(to_drop)
        except Exception:
            logger.warning(
                "compaction summarizer failed; keeping full history this round",
                exc_info=True,
            )
            return list(messages)
        self._dropped_count += len(to_drop)

        logger.info(
            "Compacted: dropped %d messages, kept %d, summary=%d tokens",
            len(to_drop),
            len(to_keep),
            count_tokens(self._rolling_summary),
        )
        # Record the trigger, range size and pre-compaction size: without this
        # an auditor cannot explain why a stretch of context disappeared.
        self._emit_trace(
            EVENT_COMPACTION,
            summary=(
                f"compaction：丢弃 {len(to_drop)} 条消息，保留 {len(to_keep)} 条，"
                f"滚动摘要 {count_tokens(self._rolling_summary)} tokens"
            ),
            dropped=len(to_drop),
            kept=len(to_keep),
            tokens={"window_before": conv_tokens, "budget": self._token_budget},
            summary_tokens=count_tokens(self._rolling_summary),
        )

        return [system_msg] + to_keep

    def build_context_prefix(self) -> str:
        """Build the context prefix injected into system prompt.

        Contains rolling summary of past conversation.
        """
        if not self._rolling_summary:
            return ""

        return (
            "\n\n## Conversation Summary (previous context)\n"
            f"{self._rolling_summary}\n"
        )

    def _find_split_point(self, conversation: list[dict[str, Any]]) -> int:
        """Find index to split: keep messages[index:] within budget.

        Keep as MUCH of the tail as fits — walk backwards from the newest
        message accumulating tokens, and stop at the first message that would
        push the window over budget. That single rule IS the policy: what fits
        is kept verbatim, what doesn't is summarized.

        There used to be a second rule here — a floor guaranteeing the newest
        N user turns survived. It was removed (2026-09-14) after an A/B run
        over 10 scenarios showed it never changed the outcome: anything that
        fits is already inside the tail window, and when it did not fit the
        floor bowed out to the budget anyway. Two moving parts that always
        moved together are one part.
        """
        n = len(conversation)

        # Walk backwards from the end, accumulating tokens
        # until we exceed budget
        kept_tokens = 0
        split = n  # start: keep everything

        for i in range(n - 1, -1, -1):
            msg_tokens = count_messages_tokens([conversation[i]])
            if kept_tokens + msg_tokens > self._token_budget:
                split = i + 1
                break
            kept_tokens += msg_tokens
        else:
            # Everything fits (shouldn't reach here if called correctly)
            return 0

        # Adjust split so the keep window doesn't start on an orphan tool
        # result (its parent assistant(tool_calls) would be in the dropped
        # side → the next API call 400s on an unpaired tool message).
        return self._align_to_safe_boundary(conversation, split)

    def _align_to_safe_boundary(
        self, conversation: list[dict[str, Any]], split: int
    ) -> int:
        """Adjust split so the kept window never starts on a ``tool`` message.

        Starting on a user / assistant / assistant(tool_calls) message is all
        API-valid (a tool_calls message's tool replies follow contiguously);
        starting on a bare ``tool`` result is not. Moving forward drops the
        whole tool group into the summarized side, keeping both halves
        internally consistent.

        Note: this deliberately does NOT align to the next 'user' message —
        in an agentic turn there may be no later user message at all, and
        walking forward to find one would push the split off the end and
        wipe the in-flight task (see maybe_compact's hard floor).
        """
        while split < len(conversation) and conversation[split].get("role") == "tool":
            split += 1
        return split

    @staticmethod
    def _strip_incomplete_tool_groups(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Remove trailing assistant(tool_calls) blocks missing tool replies.

        Returns (kept, stripped). Stripped messages belong with the dropped
        side so they can be summarized.
        """
        if not messages:
            return messages, []

        # Walk from the end: if the tail is an unresolved tool-call group,
        # peel it off. A resolved group ends with tool messages covering every
        # id; an unresolved one is assistant(tool_calls) with fewer immediate
        # tool msgs than tool_calls (or none).
        i = len(messages) - 1
        # Skip trailing non-tool / non-tool_calls noise? Only peel incomplete
        # groups at the very end (the concurrent-append case).
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            # Bare assistant(tool_calls) at end — incomplete.
            return messages[:i], messages[i:]

        # If tail is tool msgs, find the preceding assistant(tool_calls) and
        # verify coverage; if incomplete, strip the whole group.
        if messages[i].get("role") == "tool":
            j = i
            while j >= 0 and messages[j].get("role") == "tool":
                j -= 1
            if j >= 0 and messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                needed = {tc.get("id") for tc in messages[j]["tool_calls"] if tc.get("id")}
                have = {
                    messages[k].get("tool_call_id")
                    for k in range(j + 1, i + 1)
                    if messages[k].get("tool_call_id")
                }
                if not needed.issubset(have):
                    return messages[:j], messages[j:]
        return messages, []

    # Snapshot semantics are DECLARED BY TOOLS (Tool.is_snapshot /
    # snapshot_key / snapshot_keep_recent) and resolved via
    # ``_snapshot_resolver`` — the manager itself knows no tool names.
    _STUB_PREFIX = '{"ok": true, "data": "[历史快照'

    # ── Argument age-off (full args live in the tool archive) ─────────────
    # A degraded group loses its arguments WHOLESALE to a one-line
    # placeholder — never value-by-value truncation. Context holds either the
    # original or a single line; a half-readable fragment is the one shape
    # the design forbids (it is neither usable nor cheap). Old write/type
    # payloads — full file bodies, long typed text — are pure dead weight
    # once the call has already returned.
    _ARG_PLACEHOLDER_MIN_CHARS = 120  # shorter args are already one-liners
    _ARG_PLACEHOLDER_PREFIX = '{"_aged"'  # idempotence marker

    # ── Tool-result age-off (full results live in the tool archive) ───────
    # The newest tool results stay verbatim; older ones have the bulky body
    # replaced by a one-line categorical summary (memory/tool_summary.py,
    # zero model calls). The model still knows WHAT it did and whether it
    # worked; the full text is retrievable via recall_tool_result(call_id=...).
    #
    # The cut is by TOKEN, not by message count: "the last 8 results" says
    # nothing about real pressure — 8 short commands are 500 tokens, 8 fetched
    # pages are 200K. Counting tokens makes small results immune and makes
    # oversized ones get summarised immediately.
    #
    #   protect   = token_budget × 0.35   results inside it stay verbatim
    #   actionable = protect × 0.4        (≈ 14% of budget) below this, don't
    #                                     bother rewriting anything
    #   trigger   = protect + actionable (≈ 49% of budget)
    _PRUNE_PROTECT_RATIO = 0.35
    _PRUNE_MIN_ACTION_RATIO = 0.4
    _RESULT_SUMMARY_PREFIX = '{"ok": true, "data": "[历史工具结果'
    # Floor, mirroring _ARG_PLACEHOLDER_MIN_CHARS: a result shorter than this
    # is ALREADY a one-liner, and the summary adds a "[历史工具结果已压缩]"
    # marker plus a recall pointer, so collapsing it would make the payload
    # bigger. Measured: an 82-char result became 114 chars.
    _RESULT_SUMMARY_MIN_CHARS = 120

    # ── Mid-turn protection ───────────────────────────────────────────────
    # While a user turn is in flight (allow_dropping=False) the manager only
    # shrinks payloads. Past this multiple of the budget it drops anyway —
    # refusing forever would overflow the model, which is worse than a
    # summarised middle.
    _MIDTURN_HARD_RATIO = 1.5

    @staticmethod
    def _snapshot_is_empty(content: str) -> bool:
        """True when a snapshot tool returned zero controls (not a stub)."""
        text = content or ""
        if text.startswith(MemoryManager._STUB_PREFIX):
            return False
        try:
            payload = json.loads(text)
        except Exception:
            return False
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return False
        if data.get("count") == 0:
            return True
        controls = data.get("controls")
        return isinstance(controls, list) and len(controls) == 0

    def _stub_superseded_snapshots(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Replace stale snapshot-tool payloads with a small stub.

        A payload is stale when the SAME tool appears again LATER against the
        same subject (tool-declared ``snapshot_key``). Only the bulky ``tool``
        message content is swapped — assistant(tool_calls) messages, ids, and
        ordering are untouched, so tool-call pairing and the visible
        execution flow stay intact. The newest snapshot of each key is always
        kept in full; tools may also declare ``keep_recent`` to cap how many
        non-empty snapshots stay per tool. Already-stubbed messages are
        skipped (idempotent, no log spam).
        """
        # tool_call_id → (name, spec, snapshot key), from assistant tool_calls
        call_meta: dict[str, tuple[str, SnapshotSpec, str]] = {}
        spec_cache: dict[str, SnapshotSpec | None] = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                if not tc_id or tc_id in call_meta:
                    continue
                if name not in spec_cache:
                    spec_cache[name] = self._snapshot_resolver(name)
                spec = spec_cache[name]
                if spec is None:
                    continue  # unknown or non-snapshot tool → never stubbed
                try:
                    key = spec.key_fn(str(fn.get("arguments") or ""))
                except Exception:
                    key = str(fn.get("arguments") or "")
                call_meta[tc_id] = (name, spec, key)
        if not call_meta:
            return messages

        # Last non-empty tool-message index per key — empty scans (count=0)
        # must not become "the newest snapshot" and wipe a useful tree.
        last_for_key: dict[tuple[str, str], int] = {}
        last_any: dict[tuple[str, str], int] = {}
        tool_meta_idx: list[tuple[int, str, SnapshotSpec, str]] = []
        for i, m in enumerate(messages):
            if m.get("role") != "tool":
                continue
            meta = call_meta.get(m.get("tool_call_id") or "")
            if meta is None:
                continue
            if str(m.get("content") or "").startswith(self._STUB_PREFIX):
                continue  # already stubbed in an earlier round
            n, spec, key = meta
            tkey = (n, key)
            tool_meta_idx.append((i, n, spec, key))
            last_any[tkey] = i
            if not self._snapshot_is_empty(str(m.get("content") or "")):
                last_for_key[tkey] = i
        for tkey, idx in last_any.items():
            last_for_key.setdefault(tkey, idx)

        stale = set()
        for i, n, _spec, key in tool_meta_idx:
            content = str(messages[i].get("content") or "")
            if self._snapshot_is_empty(content):
                continue
            if last_for_key[(n, key)] != i:
                stale.add(i)

        # keep_recent: cap how many non-empty snapshots each TOOL keeps
        # overall (e.g. ui_inspect keeps its 3 newest trees across filters).
        full_by_tool: dict[str, tuple[SnapshotSpec, list[int]]] = {}
        for i, n, spec, _key in tool_meta_idx:
            if spec.keep_recent is None:
                continue
            if self._snapshot_is_empty(str(messages[i].get("content") or "")):
                continue
            full_by_tool.setdefault(n, (spec, []))[1].append(i)
        for _n, (spec, idxs) in full_by_tool.items():
            if len(idxs) > spec.keep_recent:
                stale.update(idxs[: -spec.keep_recent])

        if not stale:
            return messages

        out = list(messages)
        for i in stale:
            original = str(out[i].get("content") or "")
            tc_id = str(out[i].get("tool_call_id") or "")
            name = call_meta[out[i]["tool_call_id"]][0]
            pointer = (
                f"[历史快照已被后续同名 {name} 结果取代，此处省略 "
                f"{len(original)} 字符以节省上下文；需要最新状态请重新调用该工具；"
                f'需要本份完整结果可 recall_tool_result(call_id="{tc_id}")]'
            )
            out[i] = {
                **out[i],
                "content": json.dumps(
                    {"ok": True, "data": pointer}, ensure_ascii=False
                ),
            }
            logger.info(
                "Stubbed superseded %s result (%d chars → pointer stub, "
                "call_id=%s)",
                name,
                len(original),
                tc_id,
            )
        return out

    # ── Pruning: degrade whole groups outside the protect line ────────────

    def _age_off_old_groups(
        self, messages: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Pruning entry point — degrade every group past the protect line.

        ONE rule, applied to a group at a time: a group is either INTACT
        (arguments + results verbatim) or DEGRADED into two one-liners — a
        summary of the result and a placeholder for the arguments. There is no
        third shape; a group never ends up half-read.

        Which groups count as old is decided once, by token
        (:meth:`_aged_group_starts`), so both halves act on the same set and
        can never disagree about where the cut is.

        ORDER MATTERS — results first. The summary generator reads the
        arguments to write "读了 <path>" / "来自 <URL>"; hand it placeholders
        and every summary collapses to "read_file(...) → ok".

        Returns ``(results_summarized, groups_arguments_placeholdered)``.
        """
        groups = self._complete_group_indices(messages)
        old_starts = self._aged_group_starts(messages, groups)
        if not old_starts:
            return 0, 0
        results = self._age_off_old_results(messages, old_starts)
        arguments = self._age_off_old_arguments(messages, old_starts)
        return results, arguments

    # ── Argument age-off ──────────────────────────────────────────────────

    @classmethod
    def _arg_placeholder(cls, call_id: str, arguments: Any = None) -> str:
        """One-line replacement for a degraded group's ``function.arguments``.

        Must stay valid JSON — that is the API contract. Three fields, each
        earning its place:

        ``_aged``     marks it as compressed, so the model never mistakes the
                      placeholder for what it actually passed earlier;
        ``_call_id``  the key to the untouched record in tool_details.jsonl
                      (``recall_tool_result`` returns full arguments + result);
        ``_hint``     path / url / command / query, so the model still knows
                      what this call was ABOUT without paying for a recall —
                      result summaries omit it for e.g. web_search.
        """
        payload: dict[str, Any] = {"_aged": True, "_call_id": call_id}
        hint = argument_hint(arguments)
        if hint:
            payload["_hint"] = hint
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _age_off_old_arguments(
        self, messages: list[dict[str, Any]], old_starts: set[int]
    ) -> int:
        """Swap a degraded group's arguments for a one-line placeholder.

        WHOLESALE, not value by value: context holds either the original or a
        single line. Cutting every long value down to 200 chars instead leaves
        a half-read fragment — too long to be cheap, too short to be usable —
        the one shape the design forbids. It also fights the result summary
        sitting right below it, which already said "wrote notes/note_0.md".

        Skipped: short arguments (already one-liners — the placeholder would
        be bigger than what it replaces), snapshot groups (Pass 0 owns them),
        and already-aged ones (keeps this idempotent).
        """
        changed = 0
        for idx in sorted(old_starts):
            m = messages[idx]
            tcs = m.get("tool_calls") or []
            if not tcs:
                continue
            new_tcs: list[dict[str, Any]] = []
            modified = False
            for tc in tcs:
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                if self._snapshot_resolver(str(fn.get("name") or "")) is not None:
                    new_tcs.append(tc)
                    continue
                if not isinstance(raw, str) or (
                    len(raw) < self._ARG_PLACEHOLDER_MIN_CHARS
                    or raw.lstrip().startswith(self._ARG_PLACEHOLDER_PREFIX)
                ):
                    new_tcs.append(tc)
                    continue
                new_tcs.append(
                    {
                        **tc,
                        "function": {
                            **fn,
                            "arguments": self._arg_placeholder(
                                str(tc.get("id") or ""), raw
                            ),
                        },
                    }
                )
                modified = True
            if modified:
                messages[idx] = {**m, "tool_calls": new_tcs}
                changed += 1
        if changed:
            logger.info(
                "Aged off arguments in %d old tool-call group(s)", changed
            )
        return changed

    # ── Tool-result age-off (the biggest context consumer) ────────────────

    @staticmethod
    def _complete_group_indices(messages: list[dict[str, Any]]) -> list[int]:
        """Indices of assistant(tool_calls) whose tool replies are all present.

        A complete group = assistant(tool_calls) + its immediately following
        tool messages, every id paired. Incomplete groups (reply not appended
        yet) are skipped — touching them would break the API pairing.
        """
        groups: list[int] = []
        n = len(messages)
        i = 0
        while i < n:
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
                j = i + 1
                seen: set[Any] = set()
                while j < n and messages[j].get("role") == "tool":
                    seen.add(messages[j].get("tool_call_id"))
                    j += 1
                if ids and all(t in seen for t in ids):
                    groups.append(i)
                i = j
            else:
                i += 1
        return groups

    @staticmethod
    def _owning_group_index(
        messages: list[dict[str, Any]], tool_idx: int
    ) -> int | None:
        """Index of the assistant(tool_calls) owning the tool msg at ``tool_idx``.

        Tool messages always immediately follow their requesting assistant
        message, so the nearest assistant above IS the owner.
        """
        tc_id = messages[tool_idx].get("tool_call_id")
        for j in range(tool_idx - 1, -1, -1):
            m = messages[j]
            if m.get("role") != "assistant":
                continue
            tcs = m.get("tool_calls") or []
            if not tcs:
                return None
            return j if tc_id in {tc.get("id") for tc in tcs} else None
        return None

    def _age_off_old_results(
        self, messages: list[dict[str, Any]], old_starts: set[int]
    ) -> int:
        """Replace OLD tool-call results with a one-line categorical summary.

        Groups inside the token protection line keep their result verbatim
        (the model is likely still working with them). Older groups
        have the bulky result body swapped for a zero-cost summary produced
        by :mod:`agent_assistant.memory.tool_summary` — so the model still
        knows *what it did* and *whether it worked*, while the full text stays
        retrievable via ``recall_tool_result(call_id=...)``.

        Boundaries (never violated):
        - message order, ids and assistant(tool_calls) pairing are untouched;
          only the ``content`` of an old ``role="tool"`` message changes;
        - already-stubbed snapshots (Pass 0) and already-summarized results
          are skipped, making this idempotent;
        - failure summaries carry a *category*, never a raw traceback.
        """
        meta: dict[str, tuple[str, Any]] = {}
        for m in messages:
            for tc in m.get("tool_calls") or []:
                tc_id = tc.get("id")
                if not tc_id or tc_id in meta:
                    continue
                fn = tc.get("function") or {}
                meta[tc_id] = (str(fn.get("name") or ""), fn.get("arguments"))

        changed = 0
        for i, m in enumerate(messages):
            if m.get("role") != "tool":
                continue
            owner = self._owning_group_index(messages, i)
            if owner is None or owner not in old_starts:
                continue
            content = str(m.get("content") or "")
            if content.startswith(self._RESULT_SUMMARY_PREFIX) or content.startswith(
                self._STUB_PREFIX
            ):
                continue
            if len(content) < self._RESULT_SUMMARY_MIN_CHARS:
                continue  # already a one-liner; a summary would be longer
            tc_id = str(m.get("tool_call_id") or "")
            tool, args = meta.get(tc_id, ("", None))
            if not tool or self._snapshot_resolver(tool) is not None:
                # Snapshots are Pass 0's business: they follow their own
                # supersede rule (a newer shot of the same window makes the
                # older one dead weight). Collapsing one into a one-line
                # summary here would hide the control tree the model is
                # actively reading — the whole point of ui_inspect.
                continue
            summary = summarize_tool_result(tool, args, content)
            payload = (
                f"[历史工具结果已压缩] {summary}；"
                f'需要完整结果可 recall_tool_result(call_id="{tc_id}")'
            )
            messages[i] = {
                **m,
                "content": json.dumps(
                    {"ok": True, "data": payload}, ensure_ascii=False
                ),
            }
            changed += 1
        if changed:
            logger.info(
                "Aged off %d old tool result(s) into one-line summaries", changed
            )
        return changed

    def _group_cost_tokens(self, messages: list[dict[str, Any]], start: int) -> int:
        """Token cost of a WHOLE group — arguments plus results.

        Both halves are what Pruning shrinks, so both count toward the
        protect line. Measuring results alone leaves a group whose ARGUMENT
        is the payload (``write_file(content=<a whole document>)``) parked
        inside the protected window forever while it eats most of the budget —
        measured on a real run: 9,510 tokens of arguments against 1,686 of
        results, and pruning never fired at all.
        """
        total = count_messages_tokens([messages[start]])  # assistant + arguments
        i = start + 1
        while i < len(messages) and messages[i].get("role") == "tool":
            total += count_messages_tokens([messages[i]])
            i += 1
        return total

    def _aged_group_starts(
        self, messages: list[dict[str, Any]], groups: list[int]
    ) -> set[int]:
        """Which groups fall outside the token protection line.

        Walk from the newest group backwards accumulating each group's token
        cost (arguments + results): what fits inside ``protect`` stays
        verbatim, everything beyond it is a candidate.

        The action threshold is what keeps this from thrashing — if the
        candidates do not add up to ``protect × 0.4``, we leave them alone.
        Rewriting messages to reclaim a few KB costs more than it saves (a
        summariser call, a write, and noise in the event stream every round).
        """
        if not groups:
            return set()
        protect = max(1, int(self._token_budget * self._PRUNE_PROTECT_RATIO))
        min_actionable = max(1, int(protect * self._PRUNE_MIN_ACTION_RATIO))

        cumulative = 0
        candidates: set[int] = set()
        actionable = 0
        for start in reversed(groups):
            cost = self._group_cost_tokens(messages, start)
            cumulative += cost
            if cumulative <= protect:
                continue  # still inside the protected window
            candidates.add(start)
            actionable += cost

        if actionable < min_actionable:
            logger.debug(
                "pruning candidates only %d tokens (< %d threshold); skip",
                actionable,
                min_actionable,
            )
            return set()
        return candidates

    # ── Rolling summary (concatenated layers) ─────────────────────────────

    def _absorb_dropped(self, to_drop: list[dict[str, Any]]) -> None:
        """Fold dropped messages into the rolling summary by CONCATENATION.

        The dropped span becomes ONE new section appended after the existing
        layers, separated by ``---``; **older layers are never re-summarized**.
        Re-summarizing an already-summarized text is the fastest way to lose
        detail, so every layer keeps the wording it was first written with.

        (Splitting overflowing layers into an on-disk archive — instead of
        condensing them in place — is a separate, later change.)
        """
        text = self._messages_to_text(to_drop)
        new_section = self._summarizer(text) if text.strip() else ""

        sections: list[str] = []
        if self._rolling_summary:
            sections.append(self._rolling_summary)
        if new_section:
            sections.append(new_section)
        combined = "\n\n---\n\n".join(sections)

        if count_tokens(combined) > self._summary_cap:
            combined = self._fit_within_cap(combined)
        self._rolling_summary = combined

    def _fit_within_cap(self, combined: str) -> str:
        """Bring an over-cap summary back in budget by ARCHIVING old layers.

        The accumulated text is **never re-summarized**: re-summarizing an
        already-summarized text is exactly the layer-by-layer decay that the
        concatenation strategy exists to prevent. Instead the oldest layers
        move to disk and leave a pointer line behind, which the model can
        follow with ``recall_summary`` (archive + recall are designed as a
        pair — one without the other is silent data loss).

        A single oversized layer has nothing older to archive, so it is the
        one case where the summarizer is still asked to condense.
        """
        layers = split_layers(combined)
        if len(layers) <= 1:
            return self._summarizer(
                f"Condense this summary to under {self._summary_cap} tokens "
                f"while preserving key facts://n//n{combined}"
            )

        # Keep the newest layers that fit ~70% of the cap; archive the rest.
        target = max(1, int(self._summary_cap * 0.7))
        keep: list[str] = []
        used = 0
        for layer in reversed(layers):
            cost = count_tokens(layer)
            if keep and used + cost > target:
                break
            keep.insert(0, layer)
            used += cost

        archived = layers[: len(layers) - len(keep)]
        if not archived:
            return combined

        files = archive_layers(archived, conversation_id=self._session_id)
        pointer = pointer_line(files)
        parts = ([pointer] if pointer else []) + keep
        result = "\n\n---\n\n".join(parts)

        if count_tokens(result) > self._summary_cap:
            result = "\n\n---\n\n".join(([pointer] if pointer else []) + layers[-1:])
        logger.info(
            "Archived %d old summary layer(s) (%s); rolling summary now %d tokens",
            len(archived),
            files[:1] or "-",
            count_tokens(result),
        )
        # Same reasoning as the compaction event: without a record here,
        # "why did that stretch of summary disappear?" has no answer.
        self._emit_trace(
            EVENT_SUMMARY_ARCHIVED,
            summary=(
                f"摘要层归档：{len(archived)} 层移入 summaries/"
                f"（{files[0] if files else '-'}），滚动摘要压缩至 "
                f"{count_tokens(result)} tokens"
            ),
            archived_layers=len(archived),
            files=files[:3],
            summary_tokens=count_tokens(result),
        )
        return result


    def _messages_to_text(self, messages: list[dict[str, Any]]) -> str:
        """Convert messages to readable text for summarization."""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                parts.append(f"[{role}]: {content}")
            # Include tool call info briefly (name + args, so the summary
            # keeps WHAT was done — e.g. which text was typed, which app
            # launched — not just that a tool ran)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = str(fn.get("arguments", ""))[:200]
                    parts.append(
                        f"[{role} called tool]: {fn.get('name', '?')}({args})"
                    )
        return "\n".join(parts)
