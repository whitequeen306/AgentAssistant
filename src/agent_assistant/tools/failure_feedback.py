"""Compact failure feedback for the model when a tool call fails.

On ``ok=false``, the tool message includes ``error_category`` plus a short
``feedback.trace`` of recent failures in this user turn — so the next loop
round can change strategy without the user clicking anything.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

# Hints the model should follow for common failure classes.
_HINTS: dict[str, str] = {
    "empty_html": (
        "Page body was empty or blocked. Do NOT retry the same URL. "
        "Use web_search for another source, or try a different URL/domain."
    ),
    "extract_fail": (
        "Could not extract readable content. Prefer another source via web_search "
        "instead of re-reading the same page."
    ),
    "ssrf": "URL was blocked for safety. Pick a public http(s) URL.",
    "confirm_denied": (
        "User denied confirmation. Do not retry this tool without explicit user consent. "
        "Do not abandon with an idle greeting — briefly say the step was blocked and wait."
    ),
    "user_cancelled": "User cancelled. Stop the current plan; wait for new instructions.",
    "timeout": "Timed out. Try a lighter query, another URL, or a different tool.",
    "subagent_timeout": (
        "Research sub-agent died mid-run, but NO progress was lost: partial "
        "findings are already in data.summary/data.findings and on disk. "
        "Recover YOURSELF — do NOT ask the user for progress or what to do. "
        "Tell the user in one line that the sub-agent died but progress "
        "survives (briefly state what was found), then call dispatch_research "
        "with the same goal + resume_from=data.resume_from to continue. "
        "If almost done (findings_count high / turns_used near budget), you "
        "may instead finish the remaining gap yourself with web_search/"
        "read_page. Prefer re-dispatching to keep your context clean."
    ),
    "host_circuit_open": (
        "This host failed repeatedly and is circuit-broken for the rest of "
        "this run. Do NOT retry it — pick a different source/host entirely."
    ),
    "serp_fetch": (
        "That URL is a search-engine results page and cannot be fetched "
        "(anti-bot). Searching is web_search's job — call web_search instead."
    ),
    "http_4xx": "HTTP client error. Check the URL or use web_search for alternatives.",
    "http_5xx": "Remote server error. Retry once at most, then switch sources.",
    "search_empty": "Search returned nothing. Rephrase the query or change keywords.",
    "not_found": "Resource not found. Verify the path/URL or search again.",
    "connection": "Network error. Try another source; do not spam the same endpoint.",
    "rate_limit": "Rate limited. Back off; use fewer calls or another backend.",
    "auth": "Authentication failed. Do not retry with the same credentials in a loop.",
    "permission_denied": "Permission denied. Ask the user or choose another approach.",
    "unknown": "Tool failed. Change approach; do not blindly retry the same call.",
}


def classify_tool_error(error: str | None, code: int | None = None) -> str:
    """Map error text/code → stable error_category for feedback + logging."""
    e = (error or "").lower()
    if "cancel" in e:
        return "user_cancelled"
    if "denied confirmation" in e or ("confirm" in e and "denied" in e):
        return "confirm_denied"
    if "url blocked" in e or "ssrf" in e:
        return "ssrf"
    if "empty html" in e or ("empty" in e and "html" in e):
        return "empty_html"
    if "could not extract" in e or "extract readable" in e:
        return "extract_fail"
    if "no results" in e or "returned no results" in e or "search backends failed" in e:
        return "search_empty"
    if code is not None:
        if 400 <= code < 500 and code != 403:
            return "http_4xx"
        if code >= 500:
            return "http_5xx"
        if code == 403 and "blocked" in e:
            return "ssrf"
    if re.search(r"timeout|timed\s*out", e):
        return "timeout"
    if re.search(r"not\s*found|404", e):
        return "not_found"
    if re.search(r"connection|unreachable|dns|refused", e):
        return "connection"
    if re.search(r"rate\s*limit|429|throttl", e):
        return "rate_limit"
    if re.search(r"unauthorized|auth|401|api\s*key", e):
        return "auth"
    if re.search(r"permission|access\s*denied", e):
        return "permission_denied"
    return "unknown"


def _short_error(error: str | None, max_len: int = 160) -> str:
    s = re.sub(r"\s+", " ", (error or "").strip())
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _arg_digest(tool_name: str, arguments: str | dict[str, Any] | None) -> dict[str, str]:
    """Tiny non-sensitive digest from tool args (host/query only)."""
    out: dict[str, str] = {}
    if not arguments:
        return out
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
    except Exception:
        return out
    if not isinstance(args, dict):
        return out
    url = args.get("url")
    if isinstance(url, str) and url.strip():
        try:
            host = urlparse(url.strip()).hostname or ""
            if host:
                out["host"] = host
        except Exception:
            pass
    q = args.get("query") or args.get("q")
    if isinstance(q, str) and q.strip():
        q = q.strip()
        out["query"] = q if len(q) <= 80 else q[:79] + "…"
    path = args.get("path") or args.get("filename")
    if isinstance(path, str) and path.strip() and tool_name in (
        "read_file",
        "edit_file",
        "write_file",
        "list_files",
    ):
        # basename only — no full paths to the model
        base = path.replace("\\", "/").rstrip("/").split("/")[-1]
        if base:
            out["name"] = base[:80]
    return out


def make_failure_entry(
    tool_name: str,
    *,
    error: str | None,
    code: int | None = None,
    error_category: str | None = None,
    arguments: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One compact failure row for the turn trace."""
    cat = error_category or classify_tool_error(error, code)
    entry: dict[str, Any] = {
        "tool": tool_name,
        "category": cat,
        "error": _short_error(error),
    }
    if code is not None:
        entry["code"] = code
    digest = _arg_digest(tool_name, arguments)
    if digest:
        entry["args"] = digest
    return entry


def build_failure_payload(
    *,
    error: str | None,
    code: int | None = None,
    error_category: str | None = None,
    data: Any = None,
    turn_trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """JSON object written into the tool message for the model."""
    cat = error_category or classify_tool_error(error, code)
    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "error_category": cat,
        "feedback": {
            "hint": _HINTS.get(cat, _HINTS["unknown"]),
            "trace": list(turn_trace or [])[-5:],
        },
    }
    if code is not None:
        payload["code"] = code
    if data is not None:
        payload["data"] = data
    return payload
