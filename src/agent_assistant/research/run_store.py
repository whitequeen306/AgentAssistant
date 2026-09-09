"""L1/L2 artifacts for one Agentic DeepResearch run.

Layout under ``data_dir/research_runs/<run_id>/``:

- ``trace.jsonl``  — L1: append-only tool/search event log
- ``findings.md``  — L2: curated facts with citations into L1 / URLs
- ``report.md``    — final report (optional, written when the sub-agent finishes)
- ``meta.json``    — goal, timestamps, paths
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_assistant.config import settings

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def research_runs_dir() -> Path:
    d = settings.data_dir / "research_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


class ResearchRun:
    """One research session's on-disk L1/L2 store."""

    def __init__(self, goal: str, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.goal = goal
        self.root = research_runs_dir() / self.run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.root / "trace.jsonl"
        self.findings_path = self.root / "findings.md"
        self.report_path = self.root / "report.md"
        self.meta_path = self.root / "meta.json"
        # L0: full raw tool outputs (nothing is ever lost — in-context
        # stubbing only replaces the model's copy, the archive keeps all).
        self.raw_path = self.root / "raw.jsonl"
        self._event_seq = 0
        self._write_meta(status="running")

    def log_raw(self, *, tool: str, content: str, turn: int | None = None) -> None:
        """Append the FULL tool output to raw.jsonl (L0 archive)."""
        event = {
            "ts": _utc_now(),
            "turn": turn,
            "tool": tool,
            "content": content,
        }
        with self.raw_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _write_meta(self, **extra: Any) -> None:
        meta = {
            "run_id": self.run_id,
            "goal": self.goal,
            "created_at": _utc_now(),
            "trace": str(self.trace_path),
            "findings": str(self.findings_path),
            "report": str(self.report_path),
        }
        if self.meta_path.exists():
            try:
                meta = {**json.loads(self.meta_path.read_text(encoding="utf-8")), **extra}
            except (OSError, json.JSONDecodeError):
                meta.update(extra)
        else:
            meta.update(extra)
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def log_l1(
        self,
        *,
        tool: str,
        args: dict[str, Any] | None = None,
        ok: bool = True,
        summary: str = "",
        source_refs: list[str] | None = None,
        turn: int | None = None,
    ) -> str:
        """Append one L1 event; returns event id (e.g. ``E3``)."""
        self._event_seq += 1
        eid = f"E{self._event_seq}"
        event = {
            "id": eid,
            "ts": _utc_now(),
            "turn": turn,
            "tool": tool,
            "args": _clip_args(args or {}),
            "ok": ok,
            "summary": (summary or "")[:2000],
            "source_refs": source_refs or [],
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return eid

    def read_l1(self) -> list[dict[str, Any]]:
        if not self.trace_path.exists():
            return []
        events = []
        for line in self.trace_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def write_findings_md(self, findings: list[dict[str, Any]]) -> Path:
        """Write L2 findings.md from structured finding dicts."""
        lines = [
            f"# Research Findings (L2)",
            f"",
            f"- run: `{self.run_id}`",
            f"- goal: {self.goal}",
            f"- updated: {_utc_now()}",
            f"",
        ]
        if not findings:
            lines.append("_No curated findings yet._\n")
        else:
            for i, f in enumerate(findings, 1):
                fid = f.get("id") or f"F{i}"
                lines.append(f"## {fid}")
                lines.append(f"- **claim**: {f.get('claim', '').strip()}")
                sources = f.get("sources") or []
                if sources:
                    lines.append(f"- **sources**: {'; '.join(str(s) for s in sources)}")
                l1_refs = f.get("l1_refs") or []
                if l1_refs:
                    lines.append(f"- **l1**: {', '.join(str(x) for x in l1_refs)}")
                if f.get("ts"):
                    lines.append(f"- **ts**: {f['ts']}")
                lines.append("")
        self.findings_path.write_text("\n".join(lines), encoding="utf-8")
        return self.findings_path

    def consolidate_l2_from_l1(self) -> list[dict[str, Any]]:
        """Build L2 findings deterministically from L1 search/read events."""
        findings: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for ev in self.read_l1():
            refs = ev.get("source_refs") or []
            summary = (ev.get("summary") or "").strip()
            tool = ev.get("tool") or ""
            per_ref = claims_by_ref(ev)
            for ref in refs:
                if not ref or ref in seen_urls:
                    continue
                seen_urls.add(ref)
                claim = per_ref.get(ref) or summary or f"Source observed via {tool}"
                # Prefer a one-line claim from summary
                claim = claim.split("\n")[0][:400]
                findings.append({
                    "id": f"F{len(findings) + 1}",
                    "claim": claim,
                    "sources": [ref],
                    "l1_refs": [ev.get("id", "")],
                    "ts": ev.get("ts") or _utc_now(),
                })
            if tool == "read_local_source" and summary and not refs:
                findings.append({
                    "id": f"F{len(findings) + 1}",
                    "claim": summary.split("\n")[0][:400],
                    "sources": [str((ev.get("args") or {}).get("path") or "local")],
                    "l1_refs": [ev.get("id", "")],
                    "ts": ev.get("ts") or _utc_now(),
                })
        self.write_findings_md(findings)
        return findings

    def write_report(self, report: str) -> Path:
        self.report_path.write_text(report or "", encoding="utf-8")
        self._write_meta(status="done", finished_at=_utc_now())
        return self.report_path

    def mark_failed(self, error: str) -> None:
        self._write_meta(status="failed", error=error[:500], finished_at=_utc_now())


_ARG_KEYS_KEEP = {
    "query", "url", "selector", "path", "filename", "title", "tag", "goal",
}


def _clip_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if k not in _ARG_KEYS_KEEP:
            continue
        if isinstance(v, str):
            out[k] = v[:500]
        else:
            out[k] = v
    return out


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def extract_refs_from_tool_result(tool: str, result_data: Any) -> list[str]:
    """Pull URLs / local paths out of a tool result for L1 source_refs."""
    refs: list[str] = []
    if isinstance(result_data, dict):
        if tool == "web_search":
            items = result_data.get("results") or result_data.get("data") or []
            if isinstance(items, dict):
                items = items.get("results") or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("url"):
                        refs.append(str(it["url"]))
        url = result_data.get("url")
        if url:
            refs.append(str(url))
        path = result_data.get("path") or result_data.get("filename")
        if path:
            refs.append(str(path))
        # Nested data from ToolResult.to_dict()
        inner = result_data.get("data")
        if isinstance(inner, dict) and inner is not result_data:
            refs.extend(extract_refs_from_tool_result(tool, inner))
    text = json.dumps(result_data, ensure_ascii=False) if not isinstance(result_data, str) else result_data
    for m in _URL_RE.findall(text)[:12]:
        if m not in refs:
            refs.append(m)
    return refs[:20]


_SEARCH_CLAIM_SEP = " — "


def claims_by_ref(ev: dict[str, Any]) -> dict[str, str]:
    """Map each source URL → its own title for ``web_search`` L1 events.

    ``summarize_tool_result`` stores search hits as one ``title — url`` line
    per result; splitting by line lets L2 attach the RIGHT claim to each ref
    instead of repeating the joined blob for every source.
    """
    out: dict[str, str] = {}
    if (ev.get("tool") or "") != "web_search":
        return out
    for line in (ev.get("summary") or "").splitlines():
        if _SEARCH_CLAIM_SEP not in line:
            continue
        title, _, url = line.rpartition(_SEARCH_CLAIM_SEP)
        url = url.strip()
        if url.startswith("http") and title.strip():
            out[url] = title.strip()
    return out


def summarize_tool_result(tool: str, result_dict: dict[str, Any]) -> str:
    """Short text summary for an L1 event."""
    if not result_dict.get("ok", True):
        return f"FAIL: {result_dict.get('error', 'unknown')}"[:500]
    data = result_dict.get("data")
    if tool == "web_search" and isinstance(data, (list, dict)):
        items = data if isinstance(data, list) else (data.get("results") or [])
        lines = []
        if isinstance(items, list):
            for it in items[:8]:
                if isinstance(it, dict):
                    title = (it.get("title") or "").strip()
                    url = (it.get("url") or "").strip()
                    if title and url:
                        lines.append(f"{title}{_SEARCH_CLAIM_SEP}{url}")
                    elif title or url:
                        lines.append(title or url)
        return "\n".join(lines)[:800] or "search ok"
    if isinstance(data, dict):
        content = data.get("content") or data.get("text") or data.get("summary")
        if content:
            return str(content)[:1500]
    if isinstance(data, str):
        return data[:1500]
    return "ok"
