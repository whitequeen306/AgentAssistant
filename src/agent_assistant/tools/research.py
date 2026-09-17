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
from agent_assistant.tools.web_tools import (
    ReadDocumentTool,
    ReadPageTool,
    WebSearchTool,
)

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
    and retried the SAME host with a different path. After the threshold is hit
    the circuit opens for the rest of the run and the model gets an explicit
    "this host is dead, pick another source" error.
    A success resets the counter (flaky ≠ dead).

    硬失败与软失败**分开计数**，理由见 SOFT_CATEGORIES 的注释。
    """

    FAIL_THRESHOLD = 2       # 硬失败：超时 / 连接失败 / HTTP 错误
    SOFT_FAIL_THRESHOLD = 3  # 软失败：页面抓到了但读不出内容（JS 渲染 / PDF / 反爬）
    TRACKED_TOOLS = ("read_page", "extract_content", "read_document")
    # 软失败表示 host 有响应，但**内容拿不到**。这正是院校官网的典型形态
    # （异步树 / PDF / 反爬）。早期实现把它算作成功来"证明 host 活着"，结果
    # 每次失败都清零计数 → 熔断永不触发 → 子 Agent 拿着 URL 无限重试
    # （实测某次调研 12 轮里 read_page 被调 38 次，web_search 只调了 1 次）。
    # 「是否算失败」和「是否计入熔断」是两件事，必须拆开。
    SOFT_CATEGORIES = frozenset({
        "extract_fail", "empty_html", "serp_fetch", "ssrf", "host_circuit_open",
    })

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._soft: dict[str, int] = {}

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
        if not host:
            return None
        if self._fails.get(host, 0) >= self.FAIL_THRESHOLD:
            return host
        if self._soft.get(host, 0) >= self.SOFT_FAIL_THRESHOLD:
            return host
        return None

    def record(
        self, tool: str, args: dict[str, Any], ok: bool, *, soft: bool = False
    ) -> None:
        if tool not in self.TRACKED_TOOLS:
            return
        host = self._host(args)
        if not host:
            return
        if ok:
            self._fails.pop(host, None)
            self._soft.pop(host, None)
        elif soft:
            self._soft[host] = self._soft.get(host, 0) + 1
        else:
            self._fails[host] = self._fails.get(host, 0) + 1


# Per-turn LLM timeout (seconds). Tests may monkeypatch this lower.
_LLM_TURN_TIMEOUT_S = 120

#: 调研深度 → 轮次预算。**由主 Agent 按任务难度选择**（agentic：不写死流程）。
#: 早期实现对所有调研一律给 60 轮，结果"查一所学校的分数线"和"四校全字段对比"
#: 花一样的钱——更糟的是模型会把预算当目标，跑满为止。分档后快速任务几十秒收工。
RESEARCH_DEPTH_TURNS: dict[str, int] = {
    "quick": 8,       # 单一事实 / 确认一个页面上的数字
    "standard": 20,   # 常规多源调研（默认）
    "deep": 45,       # 多实体横向对比 / 需要交叉核验与冲突标注
}
DEFAULT_RESEARCH_DEPTH = "standard"

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


def _url_key(tool: str, args: dict[str, Any]) -> str | None:
    """规范化 URL，用于「同一个 URL 别读两遍」的去重键。"""
    if tool not in ("read_page", "extract_content", "read_local_source"):
        return None
    url = str(args.get("url") or args.get("path") or "").strip()
    if not url:
        return None
    try:
        from urllib.parse import urlsplit, urlunsplit

        p = urlsplit(url)
        return urlunsplit(
            (p.scheme, p.netloc.lower(), p.path.rstrip("/"), p.query, "")
        )
    except Exception:  # noqa: BLE001 — 兜底用原串
        return url


def _compact_tool_args(tool: str, args: dict[str, Any]) -> dict[str, str]:
    """卡片的流式展示只需要最关键的参数，不要把整个 args 塞进事件里。"""
    if tool == "web_search":
        return {"query": str(args.get("query") or "")[:120]}
    if tool in ("read_page", "extract_content", "read_local_source", "read_document"):
        return {"url": str(args.get("url") or args.get("path") or "")[:160]}
    if tool == "save_note":
        return {"title": str(args.get("title") or "")[:80]}
    if tool == "search_papers":
        return {"query": str(args.get("query") or "")[:120]}
    out: dict[str, str] = {}
    for key, value in list(args.items())[:2]:
        out[key] = str(value)[:80]
    return out


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
    track: str | None = None,
    depth: str | None = None,
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_progress: Callable[[str, str, int, int], None] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> ToolResult:
    """Run the deep-research sub-agent with its own agentic loop + L1/L2 artifacts.

    ``resume_from`` (run_id of an interrupted run) injects that run's salvaged
    findings into the system prompt so the sub-agent continues instead of
    restarting from zero.

    ``cancel_check`` / ``on_progress`` are injected by the SubagentManager so
    each task has its own cancellation token and task-scoped progress events;
    when omitted, the legacy turn-scoped cancel and UI push are used.

    ``track`` (考研 / 考公 / 求职 …) selects a ``TrackSpec`` whose output
    template is appended to the sub-agent prompt. ``None`` falls back to the
    user's active track, then to the default — so callers never branch on it.

    ``on_event`` (optional) is a **structured** stream for the UI research
    card: ``("thinking", {text, turn})``、``("tool_call", {tool, label, args})``、
    ``("tool_result", {tool, ok, count, sources})``、``("notice", {text})``。
    Distinct from ``on_progress``, which stays as the coarse one-line status
    (and the legacy UI push) — both may fire for the same step.

    ``depth`` (quick / standard / deep) 决定轮次预算；未传则用 standard。
    显式 ``max_turns`` 优先于 ``depth``（测试与特殊调用方需要精确控制）。
    """
    from agent_assistant.goals.registry import active_cycle_year, resolve_spec

    depth_key = (depth or DEFAULT_RESEARCH_DEPTH).strip().lower()
    if depth_key not in RESEARCH_DEPTH_TURNS:
        depth_key = DEFAULT_RESEARCH_DEPTH
    if max_turns is None:
        max_turns = RESEARCH_DEPTH_TURNS[depth_key]
    max_turns = max(2, min(int(max_turns), settings.subagent_max_turns))

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

    def _emit_event(kind: str, **payload: Any) -> None:
        """Structured stream event for the UI card. Never fatal."""
        if on_event is None:
            return
        try:
            on_event(kind, payload)
        except Exception:
            logger.exception("research on_event callback raised")
    sources = list(local_sources or [])
    run = ResearchRun(goal=goal)

    # Build sub-agent tool registry
    sub_registry = ToolRegistry()
    sub_registry.register(WebSearchTool())
    sub_registry.register(ReadPageTool())
    sub_registry.register(ReadDocumentTool())
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
    track_spec = resolve_spec(track)
    if track_spec is not None:
        extras.append(
            f"## 目标轨道：{track_spec.label}\n"
            + track_spec.render_research_template(cycle_year=active_cycle_year())
            + "\n\n对比表字段："
            + "、".join(track_spec.output_fields)
        )
    extras.append(
        "## Run artifacts\n"
        f"- L1 trace: `{run.trace_path}`\n"
        f"- L2 findings: `{run.findings_path}` (written when you finish)"
    )
    extras.append(
        f"## Budget & stopping rule\n"
        f"- depth={depth_key}, hard cap {max_turns} turns. The cap is a ceiling, "
        "NOT a target — the budget you do not spend is the point.\n"
        "- STOP AS SOON AS YOU CAN ANSWER THE GOAL. Stop early when: every "
        "requested field has a sourced value, OR the remaining gaps need "
        "private/paywalled data you cannot reach.\n"
        "- Do NOT re-fetch a URL you already read, and do NOT re-run the same "
        "query with synonyms. That is pure waste and it is detected: a repeated "
        "URL is refused.\n"
        "- If a source fails or yields nothing readable TWICE, switch to a "
        "different source instead of retrying it. Report the gap honestly.\n"
        "- Numbers that live in a table (招生人数 / 分数线 / 科目代码) are usually "
        "in a PDF or image attachment, NOT in the HTML — when read_page returns "
        "little or nothing, look for a linked .pdf / attachment and call "
        "read_document. Documents are expensive: a few per run, never twice.\n"
        "- A partial but sourced report delivered on turn 6 beats an exhaustive "
        "one that runs out of budget and returns nothing."
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
    # 本次 run 内已抓取过的 URL —— 重复调用直接拒绝，防「同一个页面反复读」
    visited_urls: set[str] = set()
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
            _emit_event(
                "notice",
                text=f"预算收尾：剩余 {remaining} 轮，停止开新线索，开始撰写报告",
                tone="warning",
                turn=turn + 1,
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

        # 思考内容（DeepSeek thinking 模式）→ 卡片里的「思考」块
        reasoning = getattr(message, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning.strip():
            _emit_event(
                "thinking", text=reasoning.strip()[:1500], turn=turn + 1
            )

        # 模型边调工具边说话（少见）→ 也流出来，别让卡片只剩冷冰冰的工具行
        if message.tool_calls and message.content and message.content.strip():
            _emit_event("say", text=message.content.strip()[:800], turn=turn + 1)

        # Tool calls → execute, log L1, continue
        if message.tool_calls:
            messages.append(message.model_dump())

            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name
                fn_args_raw = tool_call.function.arguments
                fn_args = _parse_tool_args(fn_args_raw)

                _progress(fn_name, _progress_label(fn_name, fn_args), turn + 1, max_turns)
                _emit_event(
                    "tool_call",
                    tool=fn_name,
                    label=_progress_label(fn_name, fn_args),
                    args=_compact_tool_args(fn_name, fn_args),
                    turn=turn + 1,
                )
                blocked = breaker.blocked_host(fn_name, fn_args)
                url_key = _url_key(fn_name, fn_args)
                if blocked:
                    result = ToolResult.failure(
                        f"host '{blocked}' has already failed repeatedly this run "
                        "(timeout / anti-bot / content not extractable) — circuit "
                        "is OPEN for the rest of this run. Pick a DIFFERENT "
                        "source/host; do not retry this one.",
                        error_category="host_circuit_open",
                    )
                elif url_key and url_key in visited_urls:
                    result = ToolResult.failure(
                        f"'{url_key}' was already fetched in this run — its content "
                        "is already in your context above. Do NOT fetch the same "
                        "URL again; use what you already have, or pick a genuinely "
                        "different source.",
                        error_category="duplicate_fetch",
                    )
                else:
                    if url_key:
                        visited_urls.add(url_key)
                    result = sub_registry.execute(fn_name, fn_args_raw)
                # 软失败（抓到页面但读不出内容）同样计入熔断 —— 对院校官网这类
                # JS/PDF/反爬站点，换路径重试是无效的，必须换源。
                soft_fail = (not result.ok) and (
                    result.error_category in _HostCircuitBreaker.SOFT_CATEGORIES
                )
                breaker.record(fn_name, fn_args, result.ok, soft=soft_fail)
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
                _emit_event(
                    "tool_result",
                    tool=fn_name,
                    ok=bool(result.ok),
                    turn=turn + 1,
                    count=len(refs),
                    sources=list(refs[:3]),
                    text=(result.error or "")[:160] if not result.ok else "",
                )
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
            ToolParameter(
                name="depth",
                type="string",
                description=(
                    "调研深度，自己按任务难度判断，不要一律用最重的："
                    "quick=查一个事实/确认单个页面上的数字（8 轮）；"
                    "standard=常规多源调研（20 轮，默认）；"
                    "deep=多实体横向对比、需要交叉核验与冲突标注（45 轮）。"
                    "预算不是目标——信息够了就让它早点收工，能 quick 就别 standard。"
                ),
                required=False,
            ),
            ToolParameter(
                name="track",
                type="string",
                description=(
                    "目标轨道：postgrad(考研) / civil_service(考公) / job(求职)。"
                    "选定后调研会按该场景的固定结构输出（列顺序、来源要求、"
                    "冲突标注规则），例如考研择校会固定输出八列对比表。"
                    "用户要做择校/选岗/选offer这类对比时务必传入；"
                    "不传则自动使用用户当前启用的目标轨道。"
                ),
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        goal: str = kwargs.get("goal", "").strip()
        context: str = kwargs.get("context", "").strip()
        resume_from = (kwargs.get("resume_from") or "").strip() or None
        track = (kwargs.get("track") or "").strip() or None
        depth = (kwargs.get("depth") or "").strip() or None

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
            routed = self._execute_via_manager(goal, context, resume_from, track, depth)
            if routed is not None:
                return routed

        return run_research_subagent(
            goal=goal,
            context=context,
            local_sources=local_sources,
            use_knowledge=use_knowledge,
            resume_from=resume_from,
            track=track,
            depth=depth,
        )

    @staticmethod
    def _execute_via_manager(
        goal: str,
        context: str,
        resume_from: str | None,
        track: str | None = None,
        depth: str | None = None,
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
            if track:
                spec_context["track"] = track
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
