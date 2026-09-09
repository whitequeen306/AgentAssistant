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

from agent_assistant.memory.token_counter import count_messages_tokens, count_tokens

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
                    "assistant. Condense the excerpt into a brief summary that "
                    "preserves, in priority order: (1) the user's CURRENT request "
                    "or goal, (2) which steps are already done (tool names + key "
                    "arguments + outcomes), (3) what is still pending or blocked "
                    "and why, (4) other key facts and decisions. The assistant "
                    "will resume the task from your summary alone — never omit "
                    "the goal or the pending state. Output only the summary."
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
        soft_rounds: soft limit on user/assistant pairs before compaction
        summarizer: callable that summarizes text (injectable for tests)
    """

    def __init__(
        self,
        token_budget: int = 12000,
        summary_cap: int = 1200,
        soft_rounds: int = 4,
        summarizer: Summarizer | None = None,
        snapshot_resolver: Callable[[str], SnapshotSpec | None] | None = None,
    ) -> None:
        self._token_budget = token_budget
        self._summary_cap = summary_cap
        self._soft_rounds = soft_rounds
        self._summarizer = summarizer or _default_summarizer
        self._snapshot_resolver = snapshot_resolver or _registry_snapshot_resolver

        self._rolling_summary: str = ""
        self._dropped_count: int = 0
        # Progress-report texts (from the loop's forced summary rounds) whose
        # assistant messages have not been dropped/absorbed yet. At compaction
        # time a checkpoint replaces the summarizer for everything it covers.
        self._pending_checkpoints: list[str] = []

    @property
    def rolling_summary(self) -> str:
        """Current rolling summary of dropped conversation."""
        return self._rolling_summary

    @property
    def dropped_count(self) -> int:
        """Number of messages dropped so far."""
        return self._dropped_count

    def maybe_compact(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Check token budget and compact if needed.

        Returns the (possibly truncated) message list.
        Never modifies the input list in place.

        Rules:
        - messages[0] (system prompt) is NEVER dropped
        - Keeps the most recent messages that fit in budget
        - Dropped messages are summarized into rolling_summary first
        - Message pairs (user→assistant) are kept complete
        """
        if not messages:
            return messages

        # Pass 0: stub superseded snapshot payloads (cheap, runs every round,
        # even under budget). A second ui_inspect of the same window makes the
        # first tree dead weight — replacing it with a stub often brings the
        # conversation back under budget WITHOUT dropping anything.
        messages = self._stub_superseded_snapshots(messages)
        # Pass 0b: shrink arguments in old, complete tool-call groups (also
        # every round, idempotent — full originals live in the tool archive).
        # Own shallow copy first: aging replaces list slots in place, and
        # when stubbing found nothing the list above still aliases the
        # caller's list (maybe_compact promises not to mutate its input).
        messages = list(messages)
        self._age_off_old_arguments(messages)

        system_msg = messages[0]
        conversation = messages[1:]  # everything after system prompt

        # Count tokens for conversation part only
        conv_tokens = count_messages_tokens(conversation)

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

        # Drop trailing incomplete tool-call groups from the keep window.
        # soft_rounds counting treats every message equally, so a bare
        # assistant(tool_calls) (result not yet appended — e.g. concurrent
        # turn) can land in to_keep and 400 the next API call.
        to_keep, stripped = self._strip_incomplete_tool_groups(to_keep)
        if stripped:
            to_drop = to_drop + stripped

        if not to_drop:
            return list(messages)

        # Absorb dropped messages into the rolling summary. Progress-report
        # checkpoints whose messages fall inside the dropped range are merged
        # VERBATIM (they are pre-made summaries — re-summarizing them only
        # loses detail); only the gaps between checkpoints go through the
        # summarizer. A summarizer failure (network blip) must not kill the
        # in-flight turn — skip compaction this round and retry on the next.
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

        Ensures we don't split a user/assistant pair.
        Always keeps at least the last soft_rounds pairs.
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
        split = self._align_to_safe_boundary(conversation, split)

        # Ensure we keep at least soft_rounds pairs
        min_keep = self._soft_rounds * 2  # user + assistant per round
        if n - split < min_keep and n > min_keep:
            split = max(0, n - min_keep)
            split = self._align_to_safe_boundary(conversation, split)

        return split

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
    # Newest N complete tool-call groups keep full arguments; older groups
    # get long string values shrunk (valid JSON preserved — pairing works on
    # ids, snapshot keying on short filter values). Old write/type payloads
    # (full file bodies, long typed text) are pure dead weight once done.
    _KEEP_RECENT_ARG_GROUPS = 4
    _ARGS_GROUP_MIN_CHARS = 120  # groups whose total args are below this: untouched
    _ARGS_VALUE_MAX = 200  # per-string-value cap in aged-off arguments
    _ARGS_CUT_MARK = "…[已截断]"

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

    # ── Argument age-off ──────────────────────────────────────────────────

    @classmethod
    def _shrink_args_json(cls, raw: str) -> str:
        """Cut long string values inside a JSON arguments string.

        Structure stays valid JSON (snapshot keying parses it every round;
        short filter keys like title_pattern are preserved verbatim). Unparseable
        args are returned unchanged — never risk breaking the API payload.
        """
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw
        mark = cls._ARGS_CUT_MARK

        def shrink(node: Any) -> Any:
            if isinstance(node, str):
                return node if len(node) <= cls._ARGS_VALUE_MAX else (
                    node[: cls._ARGS_VALUE_MAX] + mark
                )
            if isinstance(node, dict):
                return {k: shrink(v) for k, v in node.items()}
            if isinstance(node, list):
                return [shrink(v) for v in node]
            return node

        return json.dumps(shrink(parsed), ensure_ascii=False)

    def _age_off_old_arguments(self, messages: list[dict[str, Any]]) -> int:
        """Shrink bloated arguments in OLD, COMPLETE tool-call groups.

        A group is assistant(tool_calls) + its immediately following tool
        replies, all ids paired. The newest ``_KEEP_RECENT_ARG_GROUPS``
        groups are untouched; older ones get long string values in
        ``function.arguments`` cut to ``_ARGS_VALUE_MAX`` (in place). The
        full original arguments are in the tool archive.
        """
        groups: list[int] = []  # indices of assistant(tool_calls) messages
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

        if len(groups) <= self._KEEP_RECENT_ARG_GROUPS:
            return 0

        changed = 0
        for idx in groups[: -self._KEEP_RECENT_ARG_GROUPS]:
            m = messages[idx]
            tcs = m.get("tool_calls") or []
            total_chars = sum(
                len(str((tc.get("function") or {}).get("arguments", "")))
                for tc in tcs
            )
            if total_chars < self._ARGS_GROUP_MIN_CHARS:
                continue
            new_tcs = []
            modified = False
            for tc in tcs:
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                if not isinstance(raw, str) or len(raw) < self._ARGS_GROUP_MIN_CHARS:
                    new_tcs.append(tc)
                    continue
                shrunk = self._shrink_args_json(raw)
                if shrunk != raw:
                    modified = True
                    new_tcs.append({**tc, "function": {**fn, "arguments": shrunk}})
                else:
                    new_tcs.append(tc)
            if modified:
                messages[idx] = {**m, "tool_calls": new_tcs}
                changed += 1
        if changed:
            logger.info("Aged off arguments in %d old tool-call group(s)", changed)
        return changed

    # ── Progress checkpoints ──────────────────────────────────────────────

    def record_checkpoint(self, text: str) -> None:
        """Register a progress-report text as a pre-made summary.

        Called by the agent loop right after a forced progress-summary round.
        The checkpoint stays pending until a compaction drops its message;
        it is then merged into the rolling summary VERBATIM (no extra
        summarizer call, no extra detail loss) and covers everything dropped
        before it.
        """
        t = (text or "").strip()
        if t:
            self._pending_checkpoints.append(t)

    @property
    def pending_checkpoints(self) -> list[str]:
        """Checkpoints recorded but not yet absorbed by a compaction."""
        return list(self._pending_checkpoints)

    def _absorb_dropped(self, to_drop: list[dict[str, Any]]) -> None:
        """Fold dropped messages into the rolling summary.

        A checkpoint's report is written right AFTER the rounds it describes,
        with full live context — so everything dropped BEFORE the checkpoint
        is covered by it and never re-summarized (verbatim merge, zero extra
        loss). Only the gaps AFTER a consumed checkpoint (rounds that happened
        since the report) go through the summarizer.
        """
        remaining = list(to_drop)
        pieces: list[tuple[str, str]] = []  # ("gap"|"checkpoint", text)
        consumed: set[str] = set()
        for cp in self._pending_checkpoints:
            idx = next(
                (
                    i
                    for i, m in enumerate(remaining)
                    if m.get("role") == "assistant" and m.get("content") == cp
                ),
                None,
            )
            if idx is None:
                continue  # checkpoint message not dropped (yet) — stays pending
            if pieces:
                # gap between the previous consumed checkpoint and this one
                pieces.append(("gap", self._messages_to_text(remaining[:idx])))
            # else: everything before the FIRST checkpoint is covered by it
            pieces.append(("checkpoint", cp))
            consumed.add(cp)
            remaining = remaining[idx + 1 :]
        if remaining:
            # trailing gap after the last consumed checkpoint — or, when no
            # checkpoint fell in the dropped range at all, the whole range
            pieces.append(("gap", self._messages_to_text(remaining)))
        self._pending_checkpoints = [
            c for c in self._pending_checkpoints if c not in consumed
        ]

        # Merge: old summary, then pieces chronologically. Checkpoints go in
        # raw; each gap gets one summarizer call (gaps are small).
        sections: list[str] = []
        if self._rolling_summary:
            sections.append(self._rolling_summary)
        for kind, text in pieces:
            if kind == "checkpoint":
                sections.append(text)
            else:
                sections.append(self._summarizer(text))
        combined = "\n\n---\n\n".join(sections)

        # Cap: only when the whole summary overflows do we condense — this
        # is the single lossy step, and it hits the oldest layers first in
        # practice (newest checkpoints keep near-original wording).
        if count_tokens(combined) > self._summary_cap:
            combined = self._summarizer(
                f"Condense this summary to under {self._summary_cap} tokens "
                f"while preserving key facts:\n\n{combined}"
            )
        self._rolling_summary = combined

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
