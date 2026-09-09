"""B9: Error sanitization — strip sensitive info before it reaches the model.

Principle: full detail logged locally (DEBUG level), model only sees a safe
category message. No file paths, no stack traces, no version strings.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SanitizedError:
    """Result of sanitizing a raw error message."""

    safe_message: str  # What the model sees
    category: str  # Machine-readable category (e.g. "permission_denied")
    code: int | None = None  # Preserved error code if provided


# ─── Patterns for sensitive content ──────────────────────────────────────────

# Windows absolute paths: C:\Users\... or C:/Users/...
_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:[\\\/][^\s\"'<>|*?\n]*", re.IGNORECASE
)

# Unix absolute paths: /home/user/...
_UNIX_PATH_RE = re.compile(
    r"(?:\/[\w.\-]+){2,}[^\s\"'\n]*"
)

# Relative paths with ..
_REL_PATH_RE = re.compile(
    r"(?:\.\.[\\\/])+[^\s\"'<>|*?\n]*"
)

# Python tracebacks
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):.*?(?=\n\w+Error|\n\w+Exception|\Z)",
    re.DOTALL,
)

# PowerShell error location lines
_PS_LOCATION_RE = re.compile(
    r"At [^\n]+:\d+ char:\d+", re.IGNORECASE
)

# Version strings: "Python 3.11.9", "openai 1.35.2", "v3.11.9"
_VERSION_RE = re.compile(
    r"\b(?:Python|openai|pip|node|npm)\s+v?\d+\.\d+[\d.]*\b", re.IGNORECASE
)

# Generic semver-like patterns in error context
_SEMVER_RE = re.compile(r"\bv?\d+\.\d+\.\d+(?:\.\d+)?\b")

# File references in tracebacks: File "path", line N
_FILE_REF_RE = re.compile(
    r'File "[^"]+", line \d+', re.IGNORECASE
)


# ─── Category classification ─────────────────────────────────────────────────

_CATEGORY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("timeout", re.compile(r"timeout|timed?\s*out|deadline", re.IGNORECASE)),
    ("permission_denied", re.compile(r"permission|access\s*denied|errno\s*13|403", re.IGNORECASE)),
    ("not_found", re.compile(
        r"not\s*found|no\s*such\s*file|errno\s*2|404|does\s*not\s*exist", re.IGNORECASE)),
    ("connection", re.compile(
        r"connection|network|unreachable|dns|resolve|refused", re.IGNORECASE)),
    ("rate_limit", re.compile(r"rate\s*limit|too\s*many\s*requests|429|throttl", re.IGNORECASE)),
    ("auth", re.compile(r"unauthorized|authentication|api\s*key|invalid\s*key|401", re.IGNORECASE)),
    ("disk", re.compile(r"disk\s*full|no\s*space|quota", re.IGNORECASE)),
    ("encoding", re.compile(r"encoding|decode|codec|unicode|utf", re.IGNORECASE)),
]

# Safe messages per category (what the model sees)
_CATEGORY_MESSAGES: dict[str, str] = {
    "timeout": "Operation timed out. The target service may be slow or unavailable.",
    "permission_denied": "Permission denied. Insufficient privileges for this operation.",
    "not_found": "Resource not found. The target file or endpoint does not exist.",
    "connection": "Network/connection error. The target may be unreachable.",
    "rate_limit": "Rate limit exceeded. Too many requests — retry after a delay.",
    "auth": "Authentication failed. API key may be invalid or expired.",
    "disk": "Storage error. Insufficient disk space.",
    "encoding": "Encoding error. The content could not be decoded.",
    "unknown": "An unexpected error occurred during this operation.",
}


def sanitize_error(raw: str, *, context: str, code: int | None = None) -> SanitizedError:
    """Sanitize a raw error message for safe model consumption.

    Args:
        raw: The raw error string (exception str, stderr, etc.)
        context: Which tool/operation produced this (for logging)
        code: Optional error code to preserve

    Returns:
        SanitizedError with safe_message (for model) and category.

    Side effect:
        Logs full raw detail at DEBUG level for local debugging.
    """
    # Log full detail locally
    logger.debug("[%s] raw error: %s", context, raw)

    # Classify category
    category = _classify(raw)

    # Build safe message
    safe_message = _CATEGORY_MESSAGES.get(category, _CATEGORY_MESSAGES["unknown"])

    return SanitizedError(
        safe_message=safe_message,
        category=category,
        code=code,
    )


def _classify(raw: str) -> str:
    """Classify error into a category based on pattern matching."""
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(raw):
            return category
    return "unknown"


def public_llm_error(exc: BaseException) -> str:
    """Short Chinese message for the UI when the LLM HTTP call fails.

    Never forwards provider JSON, URLs, or keys. Full detail stays in logs.
    """
    status = getattr(exc, "status_code", None)
    low = str(exc).lower()
    if status == 402 or "insufficient balance" in low or "insufficient_quota" in low:
        return "模型服务余额不足，充值后再试。"
    if status in (401, 403) or "invalid api key" in low or "authentication" in low:
        return "模型服务认证失败，请检查 API Key。"
    if status == 429 or "rate limit" in low or "too many requests" in low:
        return "模型服务请求过于频繁，请稍后再试。"
    if status in (500, 502, 503, 504):
        return "模型服务暂时不可用，请稍后再试。"
    return "模型服务调用失败，请稍后再试。"
