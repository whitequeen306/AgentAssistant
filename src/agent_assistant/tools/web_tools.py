"""Web tools: web_search, read_page.

Per docs/06-tool-spec.md §1.18–1.19.
web_search: single provider (AnySearch, ANYSEARCH_API_KEY). Deliberately no
fallback cascade — see the comment above ANYSEARCH_ENDPOINT for why. Every
result passes a relevance gate; a degraded backend yields an explicit failure
rather than silently poisoning the research sub-agent.
read_page: robust fetch + trafilatura (with plain-HTML fallback).
"""

from __future__ import annotations

import gzip
import html as _html
import json
import logging
import os
import re
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from agent_assistant.config import env_or_dotenv
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


# ─── Search provider ──────────────────────────────────────────────────────────
#
# Single provider by design (2026-09-11). The previous 5-tier cascade
# (BoCha → Brave → SearXNG → Tavily → Bing) was removed because it failed in
# the worst possible way — *silently*:
#   - BoCha ran out of quota and only logged at DEBUG.
#   - Brave / Tavily had no key; the local SearXNG was never running.
#   - The keyless Bing fallback returned generic popular-content pages for any
#     multi-word query (a 考研择校 query came back as 哈尔滨旅游攻略) yet still
#     reported success, so the research sub-agent fed the model tourism pages
#     and burned a whole 60-turn budget cross-checking them.
# One good provider + an *explicit* failure beats five tiers of noise.

ANYSEARCH_ENDPOINT = "https://api.anysearch.com/v1/search"
ANYSEARCH_MAX_RESULTS = 8

#: 通用字——出现在几乎所有中文页面里，不具区分度，不参与相关度计算
_RELEVANCE_STOP_CHARS = set("的了和与及或在是为对关于什么怎么如何哪些一个我你他它这那")
#: 英文虚词——同样不具区分度（漏掉会明显稀释分数，英文查询尤其明显）
_RELEVANCE_STOP_WORDS = frozenset(
    {
        "of", "the", "and", "for", "with", "from", "into", "about", "how", "what",
        "is", "are", "was", "were", "to", "in", "on", "at", "by", "vs", "or",
    }
)
#: 相关度阈值：低于此值的结果视为噪声，整批零命中则判定该源不可信
_RELEVANCE_MIN_SCORE = 0.4

_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def _query_terms(query: str) -> list[str]:
    """切出有区分度的实词：中文连续串（≥2 字）+ ASCII 词（≥2 字符）。"""
    raw = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", query or "")
    terms: list[str] = []
    for term in raw:
        low = term.lower()
        if low in _RELEVANCE_STOP_WORDS:
            continue
        if all(ch in _RELEVANCE_STOP_CHARS for ch in term):
            continue
        terms.append(low)
    return terms[:12]


def _relevance_score(query: str, text: str) -> float:
    """查询实词在文本中的加权覆盖率，0~1。

    完全命中记满分。中文词额外支持「字符覆盖率 ≥0.8 时半价计入」——中文字有
    语义，容忍「招生专业目录」与「招生学科目录」这类措辞差异；**英文词不给部分
    分**，因为字母不携带语义，给了会让 "Harbin" 一个词把旅游页面拉到及格线。
    零交集即 0 —— 旅游攻略对招生查询正是零。
    """
    terms = _query_terms(query)
    if not terms:
        return 1.0
    haystack = (text or "").lower()
    total = sum(len(t) for t in terms)
    if not total:
        return 1.0
    hit = 0.0
    for term in terms:
        if term in haystack:
            hit += len(term)
            continue
        if not _CJK_CHAR_RE.search(term):
            continue  # 纯 ASCII：只认整词命中
        unique = set(term)
        covered = sum(1 for ch in unique if ch in haystack)
        if unique and covered / len(unique) >= 0.8:
            hit += len(term) * 0.5
    return hit / total


def _gate_results(query: str, results: list[dict]) -> list[dict] | None:
    """丢弃与查询无关的结果；全军覆没则返回 None（判定该源不可信）。

    没有这道闸门，降级的搜索源会把垃圾包装成「成功」喂给子 Agent —— 这正是
    2026-09-11 那次调研翻车的直接原因。宁可显式报错，也不静默投毒。
    """
    kept: list[dict] = []
    for item in results:
        text = f"{item.get('title', '')} {item.get('snippet', '')}"
        if _relevance_score(query, text) >= _RELEVANCE_MIN_SCORE:
            kept.append(item)
    if not kept:
        logger.warning(
            "搜索结果与查询零相关，已全部丢弃（query=%r，原始 %d 条）",
            query[:60],
            len(results),
        )
        return None
    if len(kept) < len(results):
        logger.info(
            "相关度闸门丢弃 %d/%d 条结果", len(results) - len(kept), len(results)
        )
    return kept


def _search_anysearch(
    query: str, api_key: str, max_results: int = ANYSEARCH_MAX_RESULTS
) -> list[dict] | None:
    """AnySearch — agent-native search infra. POST only（GET 返回 404）。

    响应结构：``{"code":0,"message":"success","data":{"results":[
    {"title":…, "url":…, "snippet":…, "content":…}]}}``

    返回 ``None`` 表示本次调用失败（额度/鉴权/网络）；调用方必须显式报错，
    不得静默降级到任何其它来源。
    """
    try:
        payload = json.dumps(
            {"query": query, "max_results": max_results}
        ).encode("utf-8")
        req = urllib.request.Request(
            ANYSEARCH_ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "AgentAssistant/0.1",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        # WARNING 而非 DEBUG：搜索后端挂掉必须在日志里看得见
        logger.warning("AnySearch HTTP %s: %s", e.code, detail)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("AnySearch unavailable: %s", e)
        return None

    if data.get("code") != 0:
        logger.warning(
            "AnySearch error code=%s message=%s",
            data.get("code"),
            data.get("message"),
        )
        return None

    items = (data.get("data") or {}).get("results") or []
    results: list[dict] = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": (item.get("title") or "").strip(),
                "url": url,
                "snippet": (item.get("snippet") or item.get("content") or "").strip(),
            }
        )
    return results or None


class WebSearchTool(Tool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Single-shot web search (AnySearch provider). "
            "When to call: 'look up X' / 'what does X mean' / 'today's news' -- single queries. "
            "Returns title + URL + snippet list. "
            "Typical next step: read the best result with read_page before summarizing. "
            "When NOT to call: multi-round search + synthesis + a report -> use dispatch_research; "
            "reading a specific page -> use read_page. "
            "Query style: for Chinese topics, phrase the query like the TITLE of the "
            "page you want ('东北大学2026年硕士研究生招生专业目录'), NOT space-separated "
            "tokens ('东北大学 2026 硕士 招生'). "
            "IMPORTANT when it FAILS: this tool has exactly one provider and does not "
            "silently substitute a degraded one. A failure means no external info was "
            "obtained — do NOT keep re-querying with synonyms; switch to read_page on a "
            "known official site (university / government domains), or tell the user "
            "plainly that the search backend is unavailable. "
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

        api_key = env_or_dotenv("ANYSEARCH_API_KEY").strip()
        if not api_key:
            return ToolResult.failure(
                "ANYSEARCH_API_KEY is not set — web search is unavailable. "
                "Add it to .env (searched: process env, CWD/.env, project root/.env, "
                "~/AgentAssistant/.env), or use read_page on a known URL instead.",
                error_category="missing_credential",
            )

        results = _search_anysearch(query, api_key)
        if not results:
            return ToolResult.failure(
                "search backend unavailable: AnySearch returned no results "
                "(quota exhausted / auth rejected / network). Do NOT retry with "
                "reworded queries — use read_page on a known official site, or "
                "report the gap honestly.",
                error_category="search_unavailable",
            )

        gated = _gate_results(query, results)
        if gated is None:
            return ToolResult.failure(
                "search returned only irrelevant results, all discarded "
                "(the backend is likely degraded). Use a more specific query, or "
                "use read_page on a known official site.",
                error_category="search_irrelevant",
            )

        return ToolResult.success(
            data={"query": query, "results": gated, "source": "anysearch"}
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


# --------------------------------------------------------------------------
# Document parsing — read_page's blind spot.
#
# 研招数据（招生人数 / 历年分数线 / 参考书目）几乎全在 PDF 附件与图片表格里，
# read_page 只能拿到 HTML 正文，拿不到这些。read_document 补上这一环：
# PDF 走「文本层 → 表格 → 扫描件 OCR」三级，图片走 OCR。
# --------------------------------------------------------------------------

_DOC_MAX_BYTES = 30 * 1024 * 1024
_DOC_MAX_PAGES = 40
_DOC_TEXT_CAP = 14000
_DOC_TABLE_ROW_CAP = 300
#: 文本层短于此值视为扫描件（纯图片 PDF），转 OCR
_OCR_MIN_CHARS = 40


def fetch_bytes(
    url: str,
    timeout: float = 30.0,
    max_bytes: int = _DOC_MAX_BYTES,
) -> tuple[str, bytes, str]:
    """Download a binary document. Returns ``(final_url, data, content_type)``.

    Raises ``ValueError`` on empty / oversized bodies — same contract as
    ``fetch_html`` so callers get 422 instead of a stack trace.
    """
    url = _encode_url(url)
    req = urllib.request.Request(url, headers=dict(_BROWSER_HEADERS))
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        final = resp.geturl() or url
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ValueError(
                f"文件过大（{int(declared) // 1024 // 1024}MB），超过 "
                f"{max_bytes // 1024 // 1024}MB 上限"
            )
        raw = resp.read(max_bytes + 1)
        raw = _decode_http_body(raw, resp.headers.get("Content-Encoding"))

    if len(raw) > max_bytes:
        raise ValueError(f"文件超过 {max_bytes // 1024 // 1024}MB 上限，已放弃")
    if not raw:
        raise ValueError("下载到空文件（站点可能拦截或链接已失效）")
    return final, raw, ctype


def _sniff_kind(data: bytes, content_type: str, url: str) -> str:
    """Best-effort format detection: magic bytes → content-type → extension."""
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] in (b"II", b"MM"):
        return "tiff"

    if "pdf" in content_type:
        return "pdf"
    if content_type.startswith("image/"):
        return content_type.split("/", 1)[1].replace("jpeg", "jpg")

    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower().lstrip(".")
    return ext or "unknown"


def _resolve_tesseract() -> str | None:
    """Locate the tesseract binary (Windows installers are not on PATH always)."""
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _ocr_image(data: bytes, lang: str = "chi_sim+eng") -> str:
    """OCR a raster image (bytes). Raises on missing deps — caller downgrades."""
    import io

    import pytesseract
    from PIL import Image

    cmd = _resolve_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return pytesseract.image_to_string(img, lang=lang) or ""


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    # 表头与单元格都要转义 —— 单元格里的 "|" 会把 Markdown 表格撑破
    head = [
        (rows[0][i] if i < len(rows[0]) else "").replace("|", "/")
        for i in range(width)
    ]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for row in rows[1:]:
        cells = [row[i] if i < len(row) else "" for i in range(width)]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    return "\n".join(lines)


def _parse_page_range(spec: str, total: int) -> list[int] | None:
    """Parse ``'1-5,8'`` → 0-based page indices. ``None`` = every page."""
    text = (spec or "").strip()
    if not text:
        return None
    picked: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                continue
            picked.extend(range(max(1, lo), min(total, hi) + 1))
        else:
            try:
                picked.append(int(part))
            except ValueError:
                continue
    unique = sorted({p for p in picked if 1 <= p <= total})
    return [p - 1 for p in unique] or None


def _pdf_extract(data: bytes, page_spec: str = "") -> dict[str, Any]:
    """Extract text + tables from a PDF, OCR-ing scanned pages.

    Returns ``{text, tables, pages, parsed, ocr_pages, warnings}``.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as e:  # pragma: no cover — dep present in practice
        raise RuntimeError(
            "PyMuPDF 未安装，无法解析 PDF（pip install pymupdf）"
        ) from e

    warnings: list[str] = []
    page_texts: list[str] = []
    tables: list[list[list[str]]] = []
    ocr_pages: list[int] = []
    row_budget = _DOC_TABLE_ROW_CAP

    with fitz.open(stream=data, filetype="pdf") as doc:
        total = doc.page_count
        wanted = _parse_page_range(page_spec, total)
        if wanted is None:
            wanted = list(range(min(total, _DOC_MAX_PAGES)))
        elif len(wanted) > _DOC_MAX_PAGES:
            warnings.append(f"请求的页数超过 {_DOC_MAX_PAGES} 页上限，已截断")
            wanted = wanted[:_DOC_MAX_PAGES]

        for index in wanted:
            page = doc[index]
            text = page.get_text("text") or ""

            # 表格层：研招目录的招生人数/科目代码几乎都在这里
            try:
                for table in page.find_tables().tables:
                    rows = table.extract() or []
                    cleaned = [
                        [
                            ("" if c is None else str(c).replace("\n", " ").strip())
                            for c in row
                        ]
                        for row in rows
                    ]
                    cleaned = [r for r in cleaned if any(c for c in r)]
                    if not cleaned:
                        continue
                    if row_budget <= 0:
                        warnings.append(f"第 {index + 1} 页表格因总量上限被跳过")
                        continue
                    if len(cleaned) > row_budget:
                        cleaned = cleaned[:row_budget]
                        warnings.append(f"第 {index + 1} 页表格被截断")
                    row_budget -= len(cleaned)
                    tables.append(cleaned)
            except Exception as e:  # noqa: BLE001 — 表格检测失败不该拖垮整页
                logger.debug("pdf table detection failed p%s: %s", index + 1, e)

            # 扫描件：文本层几乎为空 → 渲染成图再 OCR
            if len(text.strip()) < _OCR_MIN_CHARS:
                try:
                    pix = page.get_pixmap(dpi=200)
                    ocr_text = _ocr_image(pix.tobytes("png"))
                    if ocr_text.strip():
                        text = ocr_text
                        ocr_pages.append(index + 1)
                    else:
                        warnings.append(f"第 {index + 1} 页 OCR 未识别出文字")
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"第 {index + 1} 页 OCR 失败：{e}")

            page_texts.append(f"── 第 {index + 1} 页 ──\n{text}")

        if total > _DOC_MAX_PAGES:
            warnings.append(f"文档共 {total} 页，仅解析前 {_DOC_MAX_PAGES} 页")

    return {
        "text": "\n\n".join(page_texts),
        "tables": tables,
        "pages": total,
        "parsed": len(wanted),
        "ocr_pages": ocr_pages,
        "warnings": warnings,
    }


class ReadDocumentTool(Tool):
    """Fetch and parse a document attached to a web page (PDF / image / Office).

    This is the missing half of ``read_page``: 研招网的招生目录、分数线、参考
    书目几乎都以 PDF 附件或图片形式发布，HTML 正文里没有。没有它，调研子
    Agent 只能写「未获取到」。
    """

    @property
    def name(self) -> str:
        return "read_document"

    @property
    def description(self) -> str:
        return (
            "Download and parse a DOCUMENT behind a URL (PDF / image / Office) — "
            "including PDF tables and scanned pages via OCR. "
            "When to call: read_page returns little or no content but the page "
            "links to a .pdf / attachment / image; you need numbers that live in "
            "a table (招生人数, 分数线, 科目代码, 参考书目). "
            "Returns extracted tables (as Markdown) first, then page text. "
            "When NOT to call: normal HTML pages → use read_page; "
            "searching → use web_search. "
            "Cost note: PDFs are large — call at most a few per run, and never "
            "re-read the same URL (duplicates are rejected). "
            "Available to: main agent + sub-agent."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="url",
                type="string",
                description="Direct URL of the PDF / image / Office document",
            ),
            ToolParameter(
                name="pages",
                type="string",
                description=(
                    "Optional page range for PDFs, e.g. '1-5'. "
                    "Use when the document is long and you only need part of it."
                ),
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        url: str = kwargs.get("url", "").strip()
        page_spec: str = str(kwargs.get("pages") or "").strip()
        if not url:
            return ToolResult.failure("parameter 'url' is required")

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
            final_url, data, ctype = fetch_bytes(url)
        except ValueError as e:
            return ToolResult.failure(str(e), code=422, error_category="empty_html")
        except urllib.error.HTTPError as e:
            sanitized = sanitize_error(
                f"HTTP {e.code}: {e.reason}", context="read_document", code=e.code
            )
            return ToolResult.failure(sanitized.safe_message, code=e.code)
        except urllib.error.URLError as e:
            sanitized = sanitize_error(str(e.reason), context="read_document")
            return ToolResult.failure(sanitized.safe_message)
        except Exception as e:
            sanitized = sanitize_error(str(e), context="read_document")
            return ToolResult.failure(sanitized.safe_message)

        kind = _sniff_kind(data, ctype, final_url)
        tables: list[list[list[str]]] = []
        ocr_pages: list[int] = []
        warnings: list[str] = []
        pages = 0

        try:
            if kind == "pdf":
                got = _pdf_extract(data, page_spec)
                text, tables = got["text"], got["tables"]
                pages, ocr_pages, warnings = (
                    got["pages"],
                    got["ocr_pages"],
                    got["warnings"],
                )
            elif kind in ("png", "jpg", "jpeg", "gif", "webp", "tiff", "bmp"):
                text = _ocr_image(data)
                pages = 1
                ocr_pages = [1]
                if not text.strip():
                    return ToolResult.failure(
                        "图片 OCR 未识别出文字（分辨率过低或语言包缺失）",
                        code=422,
                        error_category="extract_fail",
                    )
            elif kind in ("docx", "xlsx", "xlsm", "pptx", "rtf"):
                # 复用主 Agent 的本地解析管线（先落临时文件）
                import tempfile

                from agent_assistant.tools.file_tools import extract_file_text

                with tempfile.NamedTemporaryFile(
                    suffix="." + kind, delete=False
                ) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                try:
                    text, _meta = extract_file_text(tmp_path)
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                return ToolResult.failure(
                    f"不支持的文档类型：{kind or '未知'}。"
                    "read_document 只处理 PDF / 图片 / Office；"
                    "普通网页请用 read_page。",
                    code=422,
                    error_category="extract_fail",
                )
        except RuntimeError as e:
            # 依赖缺失（PyMuPDF / tesseract）—— 明确告知，别伪装成"无内容"
            return ToolResult.failure(str(e), code=422, error_category="extract_fail")
        except Exception as e:
            sanitized = sanitize_error(str(e), context="read_document")
            return ToolResult.failure(
                sanitized.safe_message, code=422, error_category="extract_fail"
            )

        # 表格优先拼在最前 —— 研招数据真正值钱的是表格，正文可截断
        tables_md = "\n\n".join(_rows_to_markdown(t) for t in tables if t)
        body_budget = max(2000, _DOC_TEXT_CAP - len(tables_md))
        body = (text or "").strip()

        if tables_md:
            content = f"## 表格\n{tables_md}"
            if body:
                content += f"\n\n## 正文\n{body[:body_budget]}"
        else:
            content = body[:_DOC_TEXT_CAP]
            if not content:
                return ToolResult.failure(
                    "未能从文档中提取到任何文字（可能是纯扫描件但 OCR 不可用）",
                    code=422,
                    error_category="extract_fail",
                )

        result: dict[str, Any] = {
            "url": final_url,
            "kind": kind,
            "content": content,
            "tables": len(tables),
            "pages": pages,
            "truncated": len(body) > body_budget,
        }
        if ocr_pages:
            result["ocr_pages"] = ocr_pages
        if warnings:
            result["warnings"] = warnings[:6]
        return ToolResult.success(data=result)
