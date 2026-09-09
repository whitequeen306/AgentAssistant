"""Academic paper search — arXiv + Semantic Scholar (both free, no key).

Backs the 学习科研 positioning: the deep-research sub-agent (and the main
agent) call `search_papers` for literature instead of relying on generic
web search, which is noisy for scholarly queries.

Results are normalized to the same shape web_search returns
({title, url, snippet}) so downstream consumers need no special casing.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult

logger = logging.getLogger(__name__)

_TIMEOUT_S = 15
_RETRIES = 2
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AgentAssistant/0.3"
_TAG_RE = re.compile(r"<[^>]+>")


def _http_get(url: str) -> str:
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 — network hiccups are expected
            last = e
            if attempt + 1 < _RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("request failed")


def _search_arxiv(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote(query)
    url = (
        "https://export.arxiv.org/api/query?"
        f"search_query=all:{q}&start=0&max_results={limit}"
        "&sortBy=relevance"
    )
    root = ET.fromstring(_http_get(url))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", (entry.findtext("a:title", "", ns) or "")).strip()
        link = (entry.findtext("a:id", "", ns) or "").strip()
        summary = re.sub(r"\s+", " ", (entry.findtext("a:summary", "", ns) or "")).strip()
        authors = [
            (a.findtext("a:name", "", ns) or "").strip()
            for a in entry.findall("a:author", ns)
        ]
        year = (entry.findtext("a:published", "", ns) or "")[:4]
        out.append({
            "title": title,
            "url": link,
            "snippet": summary[:300],
            "source": "arxiv",
            "authors": authors[:4],
            "year": year,
        })
    return out


def _search_s2(query: str, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.quote(query)
    fields = "title,url,abstract,year,authors,venue,citationCount"
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search?"
        f"query={q}&limit={limit}&fields={fields}"
    )
    data: Any = None
    try:
        data = json.loads(_http_get(url))
    except Exception as e:  # S2 rate-limits aggressively (429) — soft-fail
        logger.debug("Semantic Scholar search failed: %s", e)
        return []
    papers = (data or {}).get("data") or []
    out: list[dict[str, Any]] = []
    for p in papers:
        authors = [a.get("name", "") for a in (p.get("authors") or [])]
        out.append({
            "title": (p.get("title") or "").strip(),
            "url": (p.get("url") or "").strip(),
            "snippet": re.sub(r"\s+", " ", p.get("abstract") or "")[:300],
            "source": "semantic_scholar",
            "authors": authors[:4],
            "year": p.get("year"),
            "citations": p.get("citationCount"),
            "venue": p.get("venue") or "",
        })
    return out


class SearchPapersTool(Tool):
    @property
    def name(self) -> str:
        return "search_papers"

    @property
    def description(self) -> str:
        return (
            "Search academic papers via arXiv and Semantic Scholar (free, "
            "no key). Returns title/url/abstract/authors/year/citations. "
            "When to call: literature review, finding prior work, checking "
            "papers the user mentions, deep research on scholarly topics. "
            "When NOT to call: general web facts/news → web_search; "
            "opening a known paper URL → read_page."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="Scholarly query (topic, keywords, or paper title)",
            ),
            ToolParameter(
                name="limit",
                type="number",
                description="Max papers per source, default 5 (max 10)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        query = (kwargs.get("query") or "").strip()
        try:
            limit = int(kwargs.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 10))

        if not query:
            return ToolResult.failure("parameter 'query' is required")

        results: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            results.extend(_search_arxiv(query, limit))
        except Exception as e:
            logger.debug("arXiv search failed: %s", e)
            errors.append(f"arxiv: {e}")
        s2 = _search_s2(query, limit)
        results.extend(s2)

        if not results:
            detail = "; ".join(errors) or "no results"
            return ToolResult.failure(
                "paper search failed: "
                + detail
                + " — fall back to web_search for scholarly sources"
            )

        return ToolResult.success(data={
            "query": query,
            "results": results,
            "count": len(results),
        })
