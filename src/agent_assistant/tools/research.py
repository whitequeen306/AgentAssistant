"""Deep-research sub-agent and dispatch_research tool.

Per docs/04-features.md §4.6 and docs/06-tool-spec.md §1.17.

The sub-agent is fully agentic:
- Has its own tool set (web_search, read_page, extract_content, save_note,
  and optionally read_local_source)
- Runs an independent agent loop with a budget cap (turns/tokens)
- Internal loop is prompt-driven, NOT code if/else
- Framework only runs the loop + enforces budget + writes L1/L2 artifacts
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from agent_assistant.agent.prompt import build_research_system_prompt
from agent_assistant.config import settings
from agent_assistant.llm.client import llm_client
from agent_assistant.research.local_sources import (
    LocalSource,
    format_sources_for_context,
    load_source_text,
    take_pending_local_sources,
)
from agent_assistant.research.run_store import (
    ResearchRun,
    claims_by_ref,
    extract_refs_from_tool_result,
    research_runs_dir,
    summarize_tool_result,
)
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.registry import ToolRegistry
from agent_assistant.tools.save_note import SaveNoteTool
from agent_assistant.tools.web_tools import ReadPageTool, WebSearchTool

logger = logging.getLogger(__name__)


# ─── Sub-agent specific tool: extract_content ─────────────────────────────────

class ExtractContentTool(Tool):
    """Extract a specific part of a web page by CSS selector (sub-agent only)."""

    @property
    def name(self) -> str:
        return "extract_content"

    @property
    def description(self) -> str:
        return (
            "Extract a specific part of a web page by CSS selector. "
            "When to call: when read_page auto-extraction misses the target block "
            "(a table/list/specific section), use a selector to specify it manually. "
            "When NOT to call: full page body → use read_page."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="url", type="string", description="URL to fetch"),
            ToolParameter(name="selector", type="string", description="CSS selector to extract"),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        url: str = kwargs.get("url", "").strip()
        selector: str = kwargs.get("selector", "").strip()

        if not url or not selector:
            return ToolResult.failure("both 'url' and 'selector' are required")

        try:
            import re

            from agent_assistant.tools.url_guard import SSRFError, validate_url
            from agent_assistant.tools.web_tools import (
                _SERP_MESSAGE,
                _serp_block,
                fetch_html,
            )

            try:
                validate_url(url)
            except SSRFError as e:
                return ToolResult.failure(str(e))

            if _serp_block(url):
                return ToolResult.failure(
                    _SERP_MESSAGE, code=422, error_category="serp_fetch"
                )

            try:
                final_url, html = fetch_html(url, timeout=20.0)
            except ValueError as e:
                return ToolResult.failure(str(e), code=422)

            try:
                from lxml import html as lxml_html  # type: ignore[import-untyped]
                from lxml.cssselect import CSSSelector  # type: ignore[import-untyped]

                doc = lxml_html.fromstring(html)
                sel = CSSSelector(selector)
                elements = sel(doc)
                texts = [el.text_content().strip() for el in elements]
                content = "\n\n".join(t for t in texts if t)
                return ToolResult.success(
                    data={
                        "content": content[:8000],
                        "matches": len(elements),
                        "url": final_url,
                    }
                )
            except ImportError:
                tag_match = re.match(r"^(\w+)", selector)
                if tag_match:
                    tag = tag_match.group(1)
                    pattern = f"<{tag}[^>]*>(.*?)</{tag}>"
                    matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
                    texts = [re.sub(r"<[^>]+>", "", m).strip() for m in matches]
                    content = "\n\n".join(t for t in texts if t)
                    return ToolResult.success(
                        data={
                            "content": content[:8000],
                            "matches": len(texts),
                            "url": final_url,
                        }
                    )

                return ToolResult.success(
                    data={"content": "", "matches": 0, "url": final_url}
                )

        except Exception:
            return ToolResult.failure("fetch failed")


class ReadLocalSourceTool(Tool):
    """Read an attached local note or file (sub-agent only, when sources enabled)."""

    def __init__(self, allowed: list[LocalSource]) -> None:
        self._allowed = {s.path: s for s in allowed}
        # also index by filename for notes
        for s in allowed:
            self._allowed[s.path.replace("\\", "/").split("/")[-1]] = s

    @property
    def name(self) -> str:
        return "read_local_source"

    @property
    def description(self) -> str:
        names = ", ".join(sorted({s.path for s in self._allowed.values()})[:20])
        return (
            "Read an attached local knowledge-base note or local file that the "
            "user enabled for this research. "
            f"Available: {names or '(none)'}. "
            "When to call: ground the report in the user's materials. "
            "When NOT to call: general web facts → use web_search/read_page."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="Note filename or absolute file path from the attached list",
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        path = (kwargs.get("path") or "").strip()
        if not path:
            return ToolResult.failure("parameter 'path' is required")
        source = self._allowed.get(path)
        if source is None:
            # try basename match
            base = path.replace("\\", "/").split("/")[-1]
            source = self._allowed.get(base)
        if source is None:
            return ToolResult.failure("path is not in the attached local sources list")
        ok, text = load_source_text(source)
        if not ok:
            return ToolResult.failure(text)
        return ToolResult.success(
            data={
                "path": source.path,
                "kind": source.kind,
                "title": source.title or source.path,
                "content": text,
            }
        )


def _parse_tool_args(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class _HostCircuitBreaker:
    """Per-run host health for URL-fetching tools (read_page/extract_content).

    A host that keeps timing out / anti-bot walling (r.jina.ai, web.archive.org,
    sogou…) used to eat turn after turn: the model was told "try another URL"
    and retried the SAME host with a different path. After ``FAIL_THRESHOLD``
    consecutive failures the circuit opens for the rest of the run and the
    model gets an explicit "this host is dead, pick another source" error.
    A success resets the counter (flaky ≠ dead).
    """

    FAIL_THRESHOLD = 2
    TRACKED_TOOLS = ("read_page", "extract_content")
    # Soft failures mean the host RESPONDED (page fetched, just nothing
    # readable / blocked by us) — proof of life, not a dead host. Only hard
    # failures (timeout / connection / HTTP error) count toward the circuit.
    SOFT_CATEGORIES = frozenset({
        "extract_fail", "empty_html", "serp_fetch", "ssrf", "host_circuit_open",
    })

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}

    @staticmethod
    def _host(args: dict[str, Any]) -> str | None:
        url = args.get("url")
        if not url or not isinstance(url, str):
            return None
        try:
            from urllib.parse import urlsplit

            return urlsplit(url).netloc.lower() or None
        except Exception:
            return None

    def blocked_host(self, tool: str, args: dict[str, Any]) -> str | None:
        if tool not in self.TRACKED_TOOLS:
            return None
        host = self._host(args)
        if host and self._fails.get(host, 0) >= self.FAIL_THRESHOLD:
            return host
        return None

    def record(self, tool: str, args: dict[str, Any], ok: bool) -> None:
        if tool not in self.TRACKED_TOOLS:
            return
        host = self._host(args)
        if not host:
            return
        if ok:
            self._fails.pop(host, None)
        else:
            self._fails[host] = self._fails.get(host, 0) + 1


# Per-turn LLM timeout (seconds). Tests may monkeypatch this lower.
_LLM_TURN_TIMEOUT_S = 120

# Wrap-up mode: with this many turns left we tell the model to stop opening
# new threads; the very last turn runs WITHOUT tools so the run always ends
# in a written report instead of vanishing mid-exploration.
_WRAP_UP_REMAINING = 3


def _looks_like_leaked_tool_markup(text: str) -> bool:
    """Final "report" that is actually raw tool-call markup.

    Some models (DeepSeek DSML) emit ``<|DSML|><invoke …>`` blocks as plain
    text when forced to answer without tools; saving that as the report gives
    the user garbage. Detect and retry with an explicit no-tools nudge.
    """
    t = text or ""
    return "DSML" in t or ("<invoke" in t and "parameter" in t)


def _failure_with_salvage(
    run: "ResearchRun",
    goal: str,
    error_msg: str,
    turn: int,
    max_turns: int,
) -> ToolResult:
    """Uniform mid-run failure contract: salvage + resume handle + a
    ready-made recovery call.

    The recovery instruction lives in the ``error`` string itself (models
    read that first — a hint buried in feedback.trace gets ignored) and
    ``data.next_action`` is a copy-paste tool call, so the main agent can
    comply without inventing anything (it once hallucinated asking the user
    for an 'agent_state.json' that doesn't exist).
    """
    findings = run.consolidate_l2_from_l1()
    return ToolResult(
        ok=False,
        error=(
            f"{error_msg}; partial findings salvaged. RECOVER YOURSELF: "
            f"call dispatch_research with the same goal and "
            f"resume_from='{run.run_id}' (see data.next_action) — do NOT ask "
            "the user for progress, you already hold it in data.summary."
        ),
        error_category="subagent_timeout",
        data={
            "summary": _partial_summary(findings),
            "findings": _inline_findings(findings),
            "resume_from": run.run_id,
            "next_action": {
                "tool": "dispatch_research",
                "args": {"goal": goal, "resume_from": run.run_id},
            },
            "turns_used": turn,
            "findings_count": len(findings),
            "l1_trace": str(run.trace_path),
            "l2_findings": str(run.findings_path),
            "l0_raw": str(run.raw_path),
        },
    )


# Sub-agent context management: without it the messages list grows unbounded
# (3 page bodies per turn), and long runs degrade — goal attention dilutes and
# the forced final turn leaks tool-call markup. Mirrors the main agent's
# stubbing: old tool results collapse to their L1 summary; recent ones keep
# full text. This is what makes a larger turn budget safe.
_TOOL_RESULT_MAX_CHARS = 6000
_KEEP_RECENT_TOOL_MSGS = 8


def _cap_tool_content(content: str) -> str:
    if len(content) <= _TOOL_RESULT_MAX_CHARS:
        return content
    return (
        content[:_TOOL_RESULT_MAX_CHARS]
        + f"\n…[truncated {len(content) - _TOOL_RESULT_MAX_CHARS} chars; "
        "key facts are in the L1 summary]"
    )


def _age_off_tool_results(
    messages: list[dict[str, Any]],
    summaries: dict[str, str],
    stubbed_ids: set[str],
) -> None:
    """Stub tool messages older than the most recent _KEEP_RECENT_TOOL_MSGS.

    Content is replaced with its L1 summary (claim — sources); the message
    itself stays so assistant(tool_calls) ↔ tool pairing is untouched.
    Stubbed ids are tracked OUTSIDE the messages so no extra keys leak into
    the API payload.
    """
    tool_positions = [
        i for i, m in enumerate(messages) if m.get("role") == "tool"
    ]
    stale = (
        tool_positions[:-_KEEP_RECENT_TOOL_MSGS]
        if len(tool_positions) > _KEEP_RECENT_TOOL_MSGS
        else []
    )
    for i in stale:
        m = messages[i]
        tc_id = m.get("tool_call_id") or ""
        if tc_id in stubbed_ids:
            continue
        summary = summaries.get(tc_id, "")
        m["content"] = json.dumps(
            {
                "ok": True,
                "stale": True,
                "note": "旧工具结果已折叠为摘要（原文在本 run 目录的 raw.jsonl，"
                "需要细节可用 read_page 重读来源）",
                "summary": summary[:1500],
            },
            ensure_ascii=False,
        )
        stubbed_ids.add(tc_id)


def _progress_label(tool: str, args: dict[str, Any]) -> str:
    """Short human-readable line for the UI research card's rolling status."""
    if tool == "web_search":
        q = (args.get("query") or "").strip()
        return f"搜索：{q[:60]}" if q else "搜索"
    if tool in ("read_page", "extract_content"):
        host = ""
        try:
            from urllib.parse import urlsplit

            host = urlsplit(args.get("url") or "").netloc
        except Exception:
            pass
        verb = "阅读页面" if tool == "read_page" else "提取内容"
        return f"{verb}：{host or (args.get('url') or '')[:60]}"
    if tool == "save_note":
        return "保存笔记"
    return tool


def _emit_progress(tool: str, label: str, turn: int, max_turns: int) -> None:
    """Live sub-agent progress for the UI research card.

    Silent during background report runs (perf/briefing) — those run on the
    report thread and must not stream into the user's active chat view. UI
    push is best-effort and must never break the research loop.
    """
    try:
        from agent_assistant.ui.bridge import api_bridge

        if api_bridge.report_silent:
            return
        api_bridge.push_event(
            "subagent_progress",
            {"tool": tool, "label": label, "turn": turn, "max_turns": max_turns},
        )
    except Exception:
        pass


def _call_llm_guarded(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    timeout_s: int | None = None,
    poll_interval: float = 0.5,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Any, str | None]:
    """LLM call in a daemon thread with timeout + cancellation polling.

    ``cancel_check`` overrides the legacy turn-scoped ``is_cancelled`` so two
    parallel researcher tasks each honor their own cancellation token.

    Returns ``(response, None)`` on success, ``(None, "timeout")`` when the
    call exceeds ``timeout_s``, ``(None, "cancelled")`` on user cancel, or
    ``(None, "<error>")`` on exception.
    """
    import threading

    if cancel_check is None:
        from agent_assistant.agent.cancellation import is_cancelled

        cancel_check = is_cancelled

    if timeout_s is None:
        timeout_s = _LLM_TURN_TIMEOUT_S

    holder: dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["response"] = llm_client.chat(messages=messages, tools=tools)
        except Exception as e:  # noqa: BLE001 — surfaced as error string
            holder["error"] = str(e)

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    elapsed = 0.0
    while t.is_alive() and elapsed < timeout_s:
        if cancel_check():
            return None, "cancelled"
        t.join(timeout=poll_interval)
        elapsed += poll_interval
    if t.is_alive():
        return None, "timeout"
    if "error" in holder:
        return None, holder["error"]
    return holder.get("response"), None


def _partial_summary(findings: list[dict[str, Any]], max_items: int = 12) -> str:
    """Short markdown digest of salvaged L2 findings for the main agent."""
    lines: list[str] = []
    for f in findings[:max_items]:
        claim = (f.get("claim") or "").strip()
        srcs = f.get("sources") or []
        src = f" ({srcs[0]})" if srcs else ""
        if claim:
            lines.append(f"- {claim}{src}")
    if len(findings) > max_items:
        lines.append(f"- … and {len(findings) - max_items} more (see l2_findings path)")
    return "\n".join(lines) or "(no findings salvaged)"


def _salvage_findings_readonly(run_id: str) -> list[dict[str, Any]]:
    """Read-only L2 consolidation from a previous run's trace (no file writes).

    Dedupes by source ref; when the same source appears again with a richer
    summary (e.g. read_page after web_search), the richer claim wins.
    """
    findings: list[dict[str, Any]] = []
    by_ref: dict[str, int] = {}
    trace = research_runs_dir() / run_id / "trace.jsonl"
    if not trace.exists():
        return findings
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        refs = ev.get("source_refs") or []
        summary = (ev.get("summary") or "").strip()
        per_ref = claims_by_ref(ev)
        for ref in refs:
            if not ref:
                continue
            claim = (
                per_ref.get(ref)
                or (summary.split("\n")[0][:400] if summary else "")
                or f"Source via {ev.get('tool')}"
            )
            if ref in by_ref:
                idx = by_ref[ref]
                if len(claim) > len(findings[idx]["claim"]):
                    findings[idx]["claim"] = claim
            else:
                by_ref[ref] = len(findings)
                findings.append({"claim": claim, "source": ref})
    return findings


def _build_resume_digest(prev_run_id: str, max_items: int = 15) -> str:
    """Continuation brief from a previous interrupted run's on-disk artifacts."""
    if not (research_runs_dir() / prev_run_id).exists():
        return ""
    findings = _salvage_findings_readonly(prev_run_id)
    if not findings:
        return ""
    parts = [
        f"## Previous attempt (run {prev_run_id}) — RESUME, do not restart",
        "",
        "An earlier research run for this same goal was interrupted. Its partial",
        "findings are below. Do NOT re-search or re-read these sources unless you",
        "must verify something specific. Fill the remaining gaps, then write the",
        "final report.",
        "",
        "### Findings so far / already-visited sources",
    ]
    for f in findings[:max_items]:
        parts.append(f"- {f['claim']} — {f['source']}")
    if len(findings) > max_items:
        parts.append(f"- … {len(findings) - max_items} more sources in the previous run's trace")
    return "\n".join(parts)


def _inline_findings(
    findings: list[dict[str, Any]], max_items: int = 15
) -> list[dict[str, Any]]:
    """Cap L2 findings for inline return; the full list lives in l2_findings.

    Shape per item: ``{claim, sources[], l1_refs[]}`` — claim + evidence, no
    raw page content (that stays in the L1 trace, addressable via l1_refs).
    """
    out: list[dict[str, Any]] = []
    for f in findings[:max_items]:
        out.append({
            "claim": (f.get("claim") or "")[:400],
            "sources": (f.get("sources") or [])[:3],
            "l1_refs": f.get("l1_refs") or [],
        })
    return out


# ─── Sub-agent loop ────────────────────────────────────────────────────────────

def run_research_subagent(
    goal: str,
    context: str = "",
    max_turns: int | None = None,
    local_sources: list[LocalSource] | None = None,
    use_knowledge: bool = False,
    resume_from: str | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[str, str, int, int], None] | None = None,
) -> ToolResult:
    """Run the deep-research sub-agent with its own agentic loop + L1/L2 artifacts.

    ``resume_from`` (run_id of an interrupted run) injects that run's salvaged
    findings into the system prompt so the sub-agent continues instead of
    restarting from zero.

    ``cancel_check`` / ``on_progress`` are injected by the SubagentManager so
    each task has its own cancellation token and task-scoped progress events;
    when omitted, the legacy turn-scoped cancel and UI push are used.
    """
    max_turns = max_turns or settings.subagent_max_turns

    if cancel_check is None:
        from agent_assistant.agent.cancellation import is_cancelled as _legacy_cancelled

        cancel_check = _legacy_cancelled

    def _progress(tool: str, label: str, turn: int, total: int) -> None:
        if on_progress is not None:
            try:
                on_progress(tool, label, turn, total)
            except Exception:
                logger.exception("research on_progress callback raised")
            return
        _emit_progress(tool, label, turn, total)
    sources = list(local_sources or [])
    run = ResearchRun(goal=goal)

    # Build sub-agent tool registry
    sub_registry = ToolRegistry()
    sub_registry.register(WebSearchTool())
    sub_registry.register(ReadPageTool())
    sub_registry.register(ExtractContentTool())
    sub_registry.register(SaveNoteTool())
    try:
        from agent_assistant.tools.academic import SearchPapersTool

        sub_registry.register(SearchPapersTool())
    except Exception:  # noqa: BLE001 — academic source optional
        logger.warning("search_papers unavailable for research sub-agent")
    if sources:
        sub_registry.register(ReadLocalSourceTool(sources))
    if use_knowledge:
        from agent_assistant.tools.knowledge_tools import SearchKnowledgeTool

        sub_registry.register(SearchKnowledgeTool())

    # Build initial messages (minimal research stack + per-run extras)
    extras: list[str] = []
    if context:
        extras.append(f"## Background Context\n{context}")
    if resume_from:
        digest = _build_resume_digest(resume_from)
        if digest:
            extras.append(digest)
        else:
            extras.append(
                f"## Previous attempt\nRun `{resume_from}` was requested for resume "
                "but no usable artifacts were found — starting fresh."
            )
    local_block = format_sources_for_context(sources)
    if local_block:
        extras.append(local_block)
    if use_knowledge:
        extras.append(
            "## Knowledge base\n"
            "The user enabled the vector knowledge base. "
            "Call `search_knowledge` to retrieve relevant uploaded documents."
        )
    extras.append(
        "## Run artifacts\n"
        f"- L1 trace: `{run.trace_path}`\n"
        f"- L2 findings: `{run.findings_path}` (written when you finish)"
    )
    system_content = build_research_system_prompt(extra="\n\n".join(extras))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"Research goal: {goal}"},
    ]

    tools = sub_registry.openai_schemas()
    run.log_l1(
        tool="run_start",
        args={
            "goal": goal,
            "local_sources": len(sources),
            "use_knowledge": use_knowledge,
        },
        summary=(
            f"start research; local_sources={len(sources)}; "
            f"use_knowledge={use_knowledge}"
        ),
        turn=0,
    )

    from agent_assistant.tools.failure_feedback import make_failure_entry

    # Failures in this research run — fed back into each failed tool message.
    turn_failures: list[dict] = []
    breaker = _HostCircuitBreaker()
    wrapup_injected = False
    # Context management: L1 summaries per tool call, for aging off old
    # tool results (see _age_off_tool_results).
    tool_summaries: dict[str, str] = {}
    stubbed_ids: set[str] = set()

    # Agentic loop with budget cap
    for turn in range(max_turns):
        if cancel_check():
            run.mark_failed("cancelled")
            return ToolResult.failure(
                "research cancelled by user", error_category="user_cancelled"
            )

        remaining = max_turns - turn
        if remaining <= _WRAP_UP_REMAINING and not wrapup_injected:
            wrapup_injected = True
            messages.append({
                "role": "system",
                "content": (
                    f"[Budget notice] Only {remaining} turns remain. STOP opening "
                    "new lines of investigation (no searches for NEW subtopics). "
                    "Finish reading what is already in flight, then WRITE THE "
                    "FINAL REPORT from what you have collected. A partial but "
                    "honest report that marks unverified items is required — "
                    "running out of budget with no report is the worst outcome."
                ),
            })
            run.log_l1(
                tool="budget_notice",
                summary=f"wrap-up mode entered with {remaining} turns left",
                turn=turn + 1,
            )
            _progress(
                "notice", "预算收尾：整理已有材料，撰写报告", turn + 1, max_turns
            )

        # Last turn: withhold tools so the model can only write the report.
        turn_tools = tools if remaining > 1 else None

        logger.debug("Sub-agent turn %d/%d (run=%s)", turn + 1, max_turns, run.run_id)

        response, llm_err = _call_llm_guarded(messages, turn_tools, cancel_check=cancel_check)

        if llm_err == "cancelled":
            run.mark_failed("cancelled")
            return ToolResult.failure(
                "research cancelled by user", error_category="user_cancelled"
            )

        if llm_err == "timeout":
            # Transient network stalls are common — retry once in place before
            # giving up the whole run.
            logger.warning(
                "Sub-agent LLM timed out (turn %d, run=%s); retrying once",
                turn + 1, run.run_id,
            )
            run.log_l1(
                tool="llm_retry",
                ok=False,
                summary=f"turn {turn + 1} LLM call timed out; retrying once",
                turn=turn + 1,
            )
            response, llm_err = _call_llm_guarded(
                messages, turn_tools, cancel_check=cancel_check
            )
            if llm_err == "cancelled":
                run.mark_failed("cancelled")
                return ToolResult.failure(
                    "research cancelled by user", error_category="user_cancelled"
                )
            if llm_err == "timeout":
                run.mark_failed(f"turn {turn + 1} LLM timeout after retry")
                return _failure_with_salvage(
                    run, goal,
                    f"sub-agent LLM timed out twice at turn {turn + 1}/{max_turns}",
                    turn + 1, max_turns,
                )

        if llm_err:
            run.mark_failed(llm_err)
            return _failure_with_salvage(
                run, goal,
                f"sub-agent LLM call failed at turn {turn + 1}/{max_turns}: {llm_err}",
                turn + 1, max_turns,
            )

        if response is None:
            run.mark_failed("LLM returned empty response")
            return _failure_with_salvage(
                run, goal,
                "sub-agent LLM returned empty response",
                turn + 1, max_turns,
            )

        choice = response.choices[0]
        message = choice.message

        # Tool calls → execute, log L1, continue
        if message.tool_calls:
            messages.append(message.model_dump())

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args_raw = tool_call.function.arguments
                fn_args = _parse_tool_args(fn_args_raw)

                _progress(fn_name, _progress_label(fn_name, fn_args), turn + 1, max_turns)
                blocked = breaker.blocked_host(fn_name, fn_args)
                if blocked:
                    result = ToolResult.failure(
                        f"host '{blocked}' already failed "
                        f"{breaker.FAIL_THRESHOLD}x in a row this run "
                        "(timeout/anti-bot) — circuit is OPEN for the rest of "
                        "this run. Pick a DIFFERENT source/host; do not retry "
                        "this one.",
                        error_category="host_circuit_open",
                    )
                else:
                    result = sub_registry.execute(fn_name, fn_args_raw)
                # Soft failures (page fetched but unreadable, SSRF/SERP blocks)
                # prove the host is alive — treat as success for the circuit.
                circuit_ok = result.ok or (
                    result.error_category in _HostCircuitBreaker.SOFT_CATEGORIES
                )
                breaker.record(fn_name, fn_args, circuit_ok)
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
                    tool_content = result.to_model_str(turn_trace=turn_failures)
                else:
                    tool_content = result.to_model_str()
                try:
                    result_dict = json.loads(tool_content)
                except Exception:
                    result_dict = result.to_dict()
                refs = extract_refs_from_tool_result(fn_name, result_dict)
                summary = summarize_tool_result(fn_name, result_dict)
                run.log_l1(
                    tool=fn_name,
                    args=fn_args,
                    ok=bool(result_dict.get("ok", True)),
                    summary=summary,
                    source_refs=refs,
                    turn=turn + 1,
                )
                # L0 archive: full tool output persisted BEFORE any in-context
                # truncation/stubbing — nothing the sub-agent saw is ever lost.
                run.log_raw(tool=fn_name, content=tool_content, turn=turn + 1)
                tool_summaries[tool_call.id] = summary
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": _cap_tool_content(tool_content),
                })
            _age_off_tool_results(messages, tool_summaries, stubbed_ids)
            continue

        # Final answer (no tool calls) → report + L2
        report = message.content or ""
        messages.append({"role": "assistant", "content": report})
        if _looks_like_leaked_tool_markup(report):
            # Model emitted raw tool-call markup as text instead of a report
            # (happens on the forced no-tools turn). Retrying on the SAME
            # 30-turn history rarely helps — the tool-call pattern is too
            # strongly conditioned. Retry on a COMPACT context: goal +
            # findings digest only. If that still leaks markup, fall back to
            # a deterministic report built from findings — report.md must
            # never be garbage.
            run.log_l1(
                tool="report_retry",
                ok=False,
                summary="final answer contained leaked tool-call markup; "
                "retrying on compact context",
                turn=turn + 1,
            )
            salvaged = run.consolidate_l2_from_l1()
            digest = "\n".join(
                f"- {f['claim']} — {', '.join(f['sources'][:2])}"
                for f in salvaged[:40]
            ) or "(no findings collected)"
            compact = [
                {
                    "role": "system",
                    "content": (
                        "你是调研报告撰写助手。根据给定的调研目标和已收集素材，"
                        "直接输出一份中文 Markdown 报告。禁止调用任何工具，"
                        "禁止输出工具调用标记。素材不足的项目明确标注「未核实」。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"调研目标：{goal}\n\n"
                        f"已收集素材（claim — 来源）：\n{digest}\n\n"
                        "请基于以上素材写最终报告：按主题组织，标注每个结论的"
                        "来源链接，未能核实的部分诚实标注。"
                    ),
                },
            ]
            retry_resp, retry_err = _call_llm_guarded(compact, None, cancel_check=cancel_check)
            retry_text = ""
            if retry_err is None and retry_resp is not None:
                retry_msg = retry_resp.choices[0].message
                if not getattr(retry_msg, "tool_calls", None):
                    retry_text = retry_msg.content or ""
            if retry_text and not _looks_like_leaked_tool_markup(retry_text):
                report = retry_text
                messages.append({"role": "assistant", "content": report})
            else:
                # Deterministic fallback: findings list IS the report.
                run.log_l1(
                    tool="report_fallback",
                    ok=False,
                    summary="retry still leaked markup; wrote deterministic "
                    "findings report",
                    turn=turn + 1,
                )
                lines = [
                    "# 调研报告（自动整理）",
                    "",
                    f"**调研目标**：{goal}",
                    "",
                    f"本次调研共核实/收集 {len(salvaged)} 条来源要点，"
                    "因预算耗尽未能完成全部核查，以下为已确认内容：",
                    "",
                ]
                lines.extend(
                    f"- {f['claim']} — {', '.join(f['sources']) or '(无链接)'}"
                    for f in salvaged
                )
                lines.append("")
                lines.append(
                    "> 本报告由系统从调研记录自动整理（模型输出异常）。"
                    "可通过 resume 续查补全未核实部分。"
                )
                report = "\n".join(lines)
                messages.append({"role": "assistant", "content": report})
        forced = remaining <= 1  # last turn ran without tools
        run.write_report(report)
        if forced:
            run._write_meta(status="done_partial")  # noqa: SLF001 — same package
        findings = run.consolidate_l2_from_l1()
        run.log_l1(
            tool="run_finish",
            summary=f"report_chars={len(report)}; findings={len(findings)}"
            + ("; forced at budget limit" if forced else ""),
            turn=turn + 1,
        )

        summary = _summarize_report(report, goal)
        return ToolResult.success(
            data={
                "summary": summary,
                "findings": _inline_findings(findings),
                "full_report_length": len(report),
                # Full report for inline display — the UI research card renders
                # this as Markdown so the deliverable is visible in-conversation
                # (capped to keep the main agent's context sane).
                "report": report[:12000],
                "report_truncated": len(report) > 12000,
                "turns_used": turn + 1,
                "budget": max_turns,
                "run_id": run.run_id,
                **(
                    {
                        "resume_from": run.run_id,
                        "next_action": {
                            "tool": "dispatch_research",
                            "args": {"goal": goal, "resume_from": run.run_id},
                        },
                    }
                    if forced
                    else {}
                ),
                "l1_trace": str(run.trace_path),
                "l2_findings": str(run.findings_path),
                "l0_raw": str(run.raw_path),
                "report_path": str(run.report_path),
                "findings_count": len(findings),
            },
            warning=(
                "report written at the budget limit — verify completeness; "
                "if gaps remain, continue with data.next_action (do NOT "
                "restart fresh and do NOT ask the user)"
                if forced
                else None
            ),
        )

    # Budget exhausted — still persist whatever L1 we have + partial L2
    findings = run.consolidate_l2_from_l1()
    run.mark_failed("budget exhausted")
    return ToolResult.success(
        data={
            "summary": (
                "[Budget exhausted — partial results only]\n\n"
                + _partial_summary(findings)
            ),
            "findings": _inline_findings(findings),
            "full_report_length": 0,
            "turns_used": max_turns,
            "budget": max_turns,
            "run_id": run.run_id,
            "resume_from": run.run_id,
            "next_action": {
                "tool": "dispatch_research",
                "args": {"goal": goal, "resume_from": run.run_id},
            },
            "l1_trace": str(run.trace_path),
            "l2_findings": str(run.findings_path),
            "l0_raw": str(run.raw_path),
            "findings_count": len(findings),
        },
        warning=(
            "budget exhausted, partial results only; continue with "
            "data.next_action (resume_from) — do NOT restart fresh and do "
            "NOT ask the user"
        ),
    )


def _summarize_report(report: str, goal: str) -> str:
    """D4: Summarize sub-agent report before returning to main agent."""
    from agent_assistant.memory.token_counter import count_tokens

    if count_tokens(report) <= 600:
        return report

    try:
        response = llm_client.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Summarize this research report into a concise brief "
                        "(under 500 tokens). Preserve: key findings, conclusions, "
                        "source URLs. Remove: verbose analysis, redundant details. "
                        "Format: structured markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Research goal: {goal}\n\nReport:\n{report}",
                },
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or report[:2000]
    except Exception as e:
        logger.warning("Report summarization failed: %s", e)
        return report[:2000] + "\n\n[... truncated ...]"


# ─── dispatch_research tool (main agent calls this) ────────────────────────────

def _legacy_tool_result(result: Any, goal: str) -> ToolResult:
    """Map a unified SubagentResult back to the legacy dispatch_research shape."""
    findings = [
        {
            "claim": item.get("claim", ""),
            "sources": [item["ref"]] if item.get("ref") else [],
            "l1_refs": [],
        }
        for item in (result.evidence or [])
        if isinstance(item, dict)
    ]
    artifacts = result.artifacts or {}
    resume_from = artifacts.get("resume_from")
    if resume_from is None and isinstance(result.partial_result, dict):
        resume_from = result.partial_result.get("resume_from")
    data: dict[str, Any] = {
        "task_id": result.task_id,
        "findings": findings,
        "turns_used": (result.stats or {}).get("turns_used"),
        "budget": (result.stats or {}).get("budget"),
        "findings_count": (result.stats or {}).get("findings_count", len(findings)),
    }
    for key in ("run_id", "l0_raw", "l1_trace", "l2_findings", "report_path"):
        if artifacts.get(key):
            data[key] = artifacts[key]
    if resume_from:
        data["resume_from"] = resume_from
        data["next_action"] = {
            "tool": "dispatch_research",
            "args": {"goal": goal, "resume_from": resume_from},
        }

    status = getattr(result.status, "value", str(result.status))
    if status == "completed":
        report = str(result.output or "")
        data["summary"] = result.summary
        data["report"] = report[:12000]
        data["report_truncated"] = len(report) > 12000
        data["full_report_length"] = len(report)
        return ToolResult.success(data=data)

    partial = result.partial_result if isinstance(result.partial_result, dict) else {}
    data["summary"] = partial.get("summary") or result.summary
    if partial.get("findings"):
        data["findings"] = partial["findings"]
        data["findings_count"] = len(partial["findings"])
    if status == "cancelled":
        return ToolResult(
            ok=False,
            error="research cancelled by user",
            error_category="user_cancelled",
            data=data,
        )
    return ToolResult(
        ok=False,
        error=result.error or "research subagent failed",
        error_category=result.error_category or "subagent_timeout",
        data=data,
    )


class DispatchResearchTool(Tool):
    @property
    def name(self) -> str:
        return "dispatch_research"

    @property
    def description(self) -> str:
        return (
            "Dispatch the deep-research sub-agent for multi-source search + "
            "synthesis, then return the report to the user in this turn. "
            "When to call (do it immediately, do not chat first): "
            "user asks to research / investigate / compare projects or products "
            "(e.g. GitHub DeepTutor vs this app), survey a topic, produce a "
            "report, or needs iterative web search + cross-check. "
            "Pass a concrete goal (+ optional context). "
            "When NOT to call: pure translate/summarize of text already in chat; "
            "single known-URL fetch (use read_page); trivial one-shot fact "
            "lookup you can answer with one web_search. "
            "If a previous run died (subagent_timeout) or exhausted its "
            "budget, progress is NOT lost — partial findings are in "
            "data.summary/data.findings, and data.next_action is a "
            "READY-MADE dispatch_research call (same goal + resume_from) "
            "that continues where it left off. Recover autonomously: one "
            "line to the user that the sub-agent died but progress survives, "
            "then invoke data.next_action as-is (preferred — keeps your "
            "context clean), or finish the small remaining gap yourself. "
            "NEVER ask the user to paste progress or decide the recovery, "
            "and NEVER fake a resume by merely rewording the goal — only "
            "resume_from carries the previous findings forward. "
            "Return shape: data.summary (narrative report for the user), "
            "data.findings[] (structured {claim, sources, l1_refs} for "
            "citation — raw page content stays in the L1 trace on disk), "
            "data.run_id / l1_trace / l2_findings for follow-up lookup. "
            "After the tool returns, present the findings to the user — "
            "do not ask 'what should I do' or greet. "
            "Writes L1 trace + L2 findings under data_dir/research_runs/."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="goal", type="string", description="Research goal"),
            ToolParameter(
                name="context",
                type="string",
                description="Background material (optional)",
                required=False,
            ),
            ToolParameter(
                name="resume_from",
                type="string",
                description=(
                    "run_id of an interrupted research run to continue "
                    "(from a prior subagent_timeout / budget-exhausted result); "
                    "omit to start fresh"
                ),
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        goal: str = kwargs.get("goal", "").strip()
        context: str = kwargs.get("context", "").strip()
        resume_from = (kwargs.get("resume_from") or "").strip() or None

        if not goal:
            return ToolResult.failure("parameter 'goal' is required")

        # UI may have attached local sources / knowledge for this turn.
        from agent_assistant.context_attachment import take_use_knowledge

        local_sources = take_pending_local_sources()
        use_knowledge = take_use_knowledge()
        if local_sources:
            extra = format_sources_for_context(local_sources)
            context = f"{context}\n\n{extra}".strip() if context else extra

        logger.info(
            "Dispatching research sub-agent: %s (local_sources=%d, kb=%s, resume=%s)",
            goal[:100],
            len(local_sources),
            use_knowledge,
            resume_from,
        )

        # Compatibility routing: with a bound conversation turn and no
        # turn-scoped attachments, run through the SubagentManager so the task
        # gets scheduling, persistence and the Cursor-style UI card. Attached
        # local sources / knowledge stay on the direct path (their one-shot
        # state lives on this thread).
        if not local_sources and not use_knowledge:
            routed = self._execute_via_manager(goal, context, resume_from)
            if routed is not None:
                return routed

        return run_research_subagent(
            goal=goal,
            context=context,
            local_sources=local_sources,
            use_knowledge=use_knowledge,
            resume_from=resume_from,
        )

    @staticmethod
    def _execute_via_manager(
        goal: str,
        context: str,
        resume_from: str | None,
    ) -> ToolResult | None:
        """Route one research task through the SubagentManager.

        Returns ``None`` when no execution context is bound (CLI/tests) or the
        manager is unavailable — callers then use the legacy direct path.
        """
        from agent_assistant.subagents.context import current_execution_context

        execution_context = current_execution_context()
        if execution_context is None:
            return None
        try:
            from agent_assistant.subagents.models import SubagentSpec
            from agent_assistant.subagents.service import get_manager

            manager = get_manager()
            spec_context: dict[str, Any] = {}
            if context:
                spec_context["background"] = context
            if resume_from:
                spec_context["resume_from"] = resume_from
            title = goal if len(goal) <= 40 else goal[:39] + "…"
            spec = SubagentSpec.create(
                conversation_id=execution_context.conversation_id,
                parent_turn_id=execution_context.parent_turn_id,
                role="researcher",
                title=title,
                goal=goal,
                context=spec_context,
            )
            (task_id,) = manager.dispatch([spec])
        except Exception:
            logger.exception("dispatch_research manager routing failed; using direct path")
            return None

        results = manager.await_tasks(
            [task_id],
            timeout=3600,
            cancel_check=execution_context.cancel_event.is_set,
        )
        if not results:
            if execution_context.cancel_event.is_set():
                manager.control(task_id, action="cancel")
                return ToolResult.failure(
                    "research cancelled by user", error_category="user_cancelled"
                )
            return ToolResult.failure(
                "research subagent did not finish in time; check the task "
                "card or retry",
                error_category="subagent_timeout",
            )
        return _legacy_tool_result(results[0], goal)
