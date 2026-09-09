"""Web tools: web_search, read_page.

Per docs/06-tool-spec.md §1.18–1.19.
web_search cascade: BoCha (BOCHA_API_KEY, CN-native) → Brave (BRAVE_API_KEY)
→ SearXNG (local, free) → Tavily (TAVILY_API_KEY) → Bing (final fallback,
no API key). Hosted APIs come first: self-hosted scraping engines get
CAPTCHA-walled from residential IPs, so they are the fallback, not the
primary (same architecture as Cursor/OpenCode/OpenClaw).
read_page: robust fetch + trafilatura (with plain-HTML fallback).
"""

from __future__ import annotations

import gzip
import html as _html
import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.sanitize import sanitize_error
from agent_assistant.tools.url_guard import SSRFError, validate_url

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # identity avoids some empty-body quirks; gzip handled if server sends it anyway
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def _decode_http_body(raw: bytes, content_encoding: str | None) -> bytes:
    """Decompress gzip/deflate when the server sends Content-Encoding."""
    enc = (content_encoding or "").lower().strip()
    if not raw:
        return raw
    try:
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:
        logger.debug("body decompress failed (%s): %s", enc, e)
    return raw


# CJK ranges: CJK unified + ext-A + compat ideographs + kana + hangul.
_CJK_RE = re.compile(
    r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]"
)


def _normalize_cjk_query(query: str) -> str:
    """Drop spaces adjacent to CJK characters.

    Search engines tokenize space-separated CJK tokens as independent
    popular-content queries ("哈尔滨工业大学 2026 招生" drifts to tourism
    spam), while a continuous phrase hits exact-match relevance. Pure-ASCII
    queries are returned unchanged.
    """
    if not _CJK_RE.search(query):
        return query
    prev = None
    while prev != query:
        prev = query
        query = re.sub(r"(?<=[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]) +", "", query)
        query = re.sub(r" +(?=[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯])", "", query)
    return query


def _encode_url(url: str) -> str:
    """Percent-encode non-ASCII characters in path/query.

    Models sometimes hand us URLs with raw CJK characters; urllib's request
    line is ASCII-only and raises ``'ascii' codec can't encode characters``.
    ``%`` stays safe so already-encoded URLs are not double-encoded.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(parts.query, safe="=&%:/?@!$'()*+,;-._~")
        fragment = urllib.parse.quote(parts.fragment, safe="%=&/:?@-._~")
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, path, query, fragment)
        )
    except Exception:
        return url


# Search-engine result pages (SERPs) are anti-bot walled; fetching them is a
# wasted turn. Searching is web_search's job.
_SERP_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.|m\.)?baidu\.com/s(?:[?/]|$)"
    r"|(?:www\.|m\.|wap\.)?sogou\.com/web(?:[?/]|$)"
    r"|(?:www\.)?google\.[^/]+/search(?:[?/]|$)"
    r"|(?:www\.)?so\.com/s(?:[?/]|$)"
    r")",
    re.IGNORECASE,
)

_SERP_MESSAGE = (
    "this URL is a search-engine results page (SERP) — anti-bot walls make "
    "it useless to fetch. Use the web_search tool to search; use read_page "
    "only for real content pages."
)


def _serp_block(url: str) -> bool:
    return bool(_SERP_RE.search(url))


def _charset_from_headers(content_type: str | None) -> str | None:
    if not content_type:
        return None
    m = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip("\"'").lower()


def fetch_html(url: str, timeout: float = 20.0) -> tuple[str, str]:
    """Download a page as HTML text.

    Returns ``(final_url, html)``. Raises ``ValueError`` when the body is
    empty (bot wall / soft-404) so callers never feed trafilatura ``None``/``""``
    — that path logs noisy ``Document is empty`` errors.
    """
    url = _encode_url(url)
    # 1) Prefer trafilatura's downloader (better UA / redirect handling).
    try:
        import trafilatura  # type: ignore[import-untyped]

        downloaded = trafilatura.fetch_url(url)
        if isinstance(downloaded, str) and downloaded.strip():
            return url, downloaded
        if downloaded is not None:
            logger.debug("trafilatura.fetch_url returned empty for %s", url)
    except Exception as e:
        logger.debug("trafilatura.fetch_url failed for %s: %s", url, e)

    # 2) urllib fallback with browser-like headers + gzip support.
    req = urllib.request.Request(url, headers=dict(_BROWSER_HEADERS))
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        final = resp.geturl() or url
        raw = _decode_http_body(resp.read(), resp.headers.get("Content-Encoding"))
        charset = _charset_from_headers(resp.headers.get("Content-Type")) or "utf-8"
        try:
            html = raw.decode(charset, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")

    if not html or not html.strip():
        raise ValueError(
            "page returned empty HTML (site may block bots, require JS, or be unavailable)"
        )
    return final, html


def extract_main_text(html: str) -> tuple[str, str]:
    """Extract readable main text from HTML.

    Returns ``(text, method)`` where method is ``trafilatura`` or ``basic``.
    Never calls trafilatura on empty input (avoids lxml 'Document is empty' spam).
    """
    if not html or not html.strip():
        return "", "empty"

    try:
        import trafilatura  # type: ignore[import-untyped]

        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        )
        if text and len(text.strip()) > 50:
            return text.strip(), "trafilatura"
    except ImportError:
        pass
    except Exception as e:
        logger.debug("trafilatura.extract failed: %s", e)

    clean = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = _html.unescape(re.sub(r"\s+", " ", clean)).strip()
    return clean, "basic"


def _search_searxng(query: str, base_url: str = "http://localhost:8888") -> list[dict] | None:
    """Try SearXNG local instance. Returns list of results or None if unavailable."""
    try:
        url = f"{base_url}/search?q={urllib.request.quote(query)}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "AgentAssistant/0.1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("results", [])[:8]:
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                })
            return results if results else None
    except Exception as e:
        logger.debug("SearXNG unavailable: %s", e)
        return None


def _search_bocha(query: str, api_key: str) -> list[dict] | None:
    """Primary: BoCha web search API (CN-native, semantic-reranked).

    POST https://api.bochaai.com/v1/web-search — free resource pack from
    open.bochaai.com. ``summary: true`` gives per-result AI summaries.
    """
    try:
        payload = json.dumps({
            "query": query,
            "summary": True,
            "count": 10,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.bochaai.com/v1/web-search",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AgentAssistant/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 200:
            logger.debug("BoCha returned code=%s msg=%s", data.get("code"), data.get("msg"))
            return None
        pages = (data.get("data") or {}).get("webPages") or {}
        results = []
        for item in pages.get("value", []):
            url = item.get("url", "")
            if not url:
                continue
            results.append({
                "title": item.get("name", ""),
                "url": url,
                "snippet": item.get("summary") or item.get("snippet", ""),
            })
        return results if results else None
    except Exception as e:
        logger.debug("BoCha unavailable: %s", e)
        return None


def _search_brave(query: str, api_key: str) -> list[dict] | None:
    """Brave Search API (free tier 2000 req/mo; OpenClaw's default provider).

    CJK queries get zh-hans/CN market params; Latin queries stay en-US.
    """
    try:
        params = {"q": query, "count": "10"}
        if _CJK_RE.search(query):
            params.update({"search_lang": "zh-hans", "country": "CN", "ui_lang": "zh-CN"})
        else:
            params.update({"search_lang": "en", "country": "US"})
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
                "User-Agent": "AgentAssistant/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for item in (data.get("web") or {}).get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return results if results else None
    except Exception as e:
        logger.debug("Brave unavailable: %s", e)
        return None


def _search_tavily(query: str, api_key: str) -> list[dict] | None:
    """Fallback: Tavily search API."""
    try:
        url = "https://api.tavily.com/search"
        payload = json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": 8,
            "include_answer": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "AgentAssistant/0.1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("results", []):
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                })
            return results if results else None
    except Exception as e:
        logger.debug("Tavily unavailable: %s", e)
        return None


def _bing_request(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _bing_market(query: str) -> str:
    return "zh-CN" if _CJK_RE.search(query) else "en-US"


def _parse_bing_rss(xml_text: str) -> list[dict]:
    results: list[dict] = []
    for item in re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)[:8]:

        def _grab(tag: str) -> str:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", item, re.DOTALL)
            return m.group(1).strip() if m else ""

        title = _html.unescape(re.sub(r"<[^>]+>", "", _grab("title"))).strip()
        link = _grab("link")
        desc = _html.unescape(re.sub(r"<[^>]+>", "", _grab("description"))).strip()
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": desc})
    return results


def _parse_bing_html(html: str) -> list[dict]:
    results: list[dict] = []
    blocks = re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re.DOTALL)
    for block in blocks[:8]:
        h2_match = re.search(
            r'<h2[^>]*><a[^>]*href="([^"]*)"[^>]*>(.*?)</a></h2>',
            block, re.DOTALL,
        )
        if not h2_match:
            continue
        href = h2_match.group(1)
        raw_title = re.sub(r"<[^>]+>", "", h2_match.group(2)).strip()
        if not raw_title:
            continue
        snippet_match = re.search(
            r'<(?:p|div)[^>]*class="(?:b_lineclamp2|b_caption)"[^>]*>(.*?)</(?:p|div)>',
            block, re.DOTALL,
        ) or re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        raw_snippet = ""
        if snippet_match:
            raw_snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
        results.append({
            "title": _html.unescape(raw_title),
            "url": _html.unescape(href),
            "snippet": _html.unescape(raw_snippet),
        })
    return results


def _search_bing(query: str) -> list[dict] | None:
    """Final fallback: Bing web search (no API key, zero setup).

    Uses the RSS endpoint (stable XML beats scraping ``b_algo`` HTML), and
    normalizes CJK queries to continuous phrases — spaced CJK tokens make
    Bing drift to popular-content spam instead of exact matches.
    """
    q = _normalize_cjk_query(query)
    mkt = _bing_market(q)
    base = f"https://www.bing.com/search?q={urllib.parse.quote(q)}&mkt={mkt}&count=20"
    try:
        results = _parse_bing_rss(_bing_request(base + "&format=rss"))
        if results:
            return results
    except Exception as e:
        logger.debug("Bing RSS unavailable: %s", e)
    try:
        results = _parse_bing_html(_bing_request(base))
        return results if results else None
    except Exception as e:
        logger.debug("Bing unavailable: %s", e)
        return None


class WebSearchTool(Tool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Single-shot web search (BoCha -> Brave -> SearXNG -> Tavily -> Bing cascade). "
            "When to call: 'look up X' / 'what does X mean' / 'today's news' -- single queries. "
            "Returns title + URL + snippet list. "
            "Typical next step: read the best result with read_page before summarizing. "
            "When NOT to call: multi-round search + synthesis + a report -> use dispatch_research; "
            "reading a specific page -> use read_page. "
            "Query style: for Chinese topics, phrase the query like the TITLE of the "
            "page you want ('东北大学2026年硕士研究生招生专业目录'), NOT space-separated "
            "tokens ('东北大学 2026 硕士 招生') — telegraphic CJK queries degrade into "
            "popular-content spam. "
            "Available to: main agent + sub-agent."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="query", type="string", description="Search query"),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        query: str = kwargs.get("query", "").strip()
        if not query:
            return ToolResult.failure("parameter 'query' is required")

        # 1. BoCha (CN-native hosted API — needs BOCHA_API_KEY in .env)
        bocha_key = os.environ.get("BOCHA_API_KEY", "")
        if bocha_key:
            results = _search_bocha(query, bocha_key)
            if results:
                return ToolResult.success(
                    data={"query": query, "results": results, "source": "bocha"}
                )

        # 2. Brave (hosted API, free tier — needs BRAVE_API_KEY in .env)
        brave_key = os.environ.get("BRAVE_API_KEY", "")
        if brave_key:
            results = _search_brave(query, brave_key)
            if results:
                return ToolResult.success(
                    data={"query": query, "results": results, "source": "brave"}
                )

        # 3. SearXNG (local, free — but engines get CAPTCHA-walled from
        #    residential IPs, so it is a bonus layer, not the primary)
        results = _search_searxng(query)
        if results:
            return ToolResult.success(
                data={"query": query, "results": results, "source": "searxng"}
            )

        # 4. Tavily (needs TAVILY_API_KEY in .env)
        tavily_key = os.environ.get("TAVILY_API_KEY", "")
        if tavily_key:
            results = _search_tavily(query, tavily_key)
            if results:
                return ToolResult.success(
                    data={"query": query, "results": results, "source": "tavily"}
                )

        # 5. Final fallback: Bing (no API key, always available)
        results = _search_bing(query)
        if results:
            return ToolResult.success(
                data={"query": query, "results": results, "source": "bing"}
            )

        return ToolResult.failure(
            "all search backends failed — no BOCHA_API_KEY / BRAVE_API_KEY / "
            "TAVILY_API_KEY set, SearXNG (localhost:8888) down, and Bing "
            "returned no results. Check your network connection."
        )


class ReadPageTool(Tool):
    @property
    def name(self) -> str:
        return "read_page"

    @property
    def description(self) -> str:
        return (
            "Read the main text of a single web page (auto-extracts main content, strips nav/ads). "
            "When to call: 'summarize this article https://...' / 'read this link'; "
            "often the next step after web_search. "
            "Returns body text (truncated if too long). "
            "If the user wants to keep it, only call save_note after they agree. "
            "When NOT to call: deep multi-page research → use dispatch_research; "
            "searching keywords → use web_search (NEVER fetch search-engine "
            "result pages like baidu.com/s or sogou.com/web — they are "
            "anti-bot walled and will be rejected). "
            "Available to: main agent + sub-agent."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="url", type="string", description="URL to read"),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        url: str = kwargs.get("url", "").strip()
        if not url:
            return ToolResult.failure("parameter 'url' is required")

        # B8: SSRF protection — block private/localhost/non-http
        try:
            validate_url(url)
        except SSRFError as e:
            return ToolResult.failure(
                f"URL blocked: {e.reason}", code=403, error_category="ssrf"
            )

        if _serp_block(url):
            return ToolResult.failure(
                _SERP_MESSAGE, code=422, error_category="serp_fetch"
            )

        try:
            final_url, html = fetch_html(url, timeout=20.0)
            text, method = extract_main_text(html)
            if not text or len(text) < 50:
                return ToolResult.failure(
                    "could not extract readable content — page may be empty, "
                    "JS-rendered, or blocked. Try another URL or web_search.",
                    code=422,
                    error_category="extract_fail",
                )
            return ToolResult.success(
                data={
                    "url": final_url,
                    "content": text[:8000],
                    "truncated": len(text) > 8000,
                    "extraction": method,
                }
            )

        except ValueError as e:
            # Empty HTML / soft failures from fetch_html — no stack spam.
            return ToolResult.failure(
                str(e), code=422, error_category="empty_html"
            )
        except urllib.error.HTTPError as e:
            sanitized = sanitize_error(f"HTTP {e.code}: {e.reason}", context="read_page", code=e.code)
            return ToolResult.failure(sanitized.safe_message, code=e.code)
        except urllib.error.URLError as e:
            sanitized = sanitize_error(str(e.reason), context="read_page")
            return ToolResult.failure(sanitized.safe_message)
        except Exception as e:
            sanitized = sanitize_error(str(e), context="read_page")
            return ToolResult.failure(sanitized.safe_message)
