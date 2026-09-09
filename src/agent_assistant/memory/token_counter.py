"""Token counting utilities for budget management.

Non-CJK text: tiktoken cl100k_base (GPT-4 approximation for DeepSeek).
CJK text: calibrated per-char factor instead. cl100k runs ~0.7-1.0 tokens
per Chinese character while DeepSeek's tokenizer runs ~0.6 (their documented
figure) — counting CJK through tiktoken made CJK-heavy conversations look
30-60% fatter than they were, firing compaction (and its lossy summarizer)
far too early. ``settings.token_cjk_factor`` is the calibration knob.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from agent_assistant.config import settings

logger = logging.getLogger(__name__)

# Per-message overhead (role, separators) in tokens
_MSG_OVERHEAD = 4  # <im_start>{role}\n ... \n<im_end>
_REPLY_PRIMING = 2  # assistant reply priming

# CJK ideographs, extensions A/B, CJK punctuation, fullwidth forms — the
# character classes where cl100k meaningfully overcounts vs DeepSeek.
_CJK_RE = re.compile(
    "[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
    "\U00020000-\U0002a6df]"
)


@lru_cache(maxsize=1)
def _get_encoding():
    """Lazy-load tiktoken encoding (avoids import-time cost)."""
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens in a plain text string.

    CJK characters are estimated at ``settings.token_cjk_factor`` tokens per
    char (DeepSeek-calibrated); the remaining text is counted by tiktoken.
    """
    if not text:
        return 0

    non_cjk = _CJK_RE.sub("", text)
    cjk_chars = len(text) - len(non_cjk)
    if cjk_chars and not non_cjk.strip():
        # All-CJK (CJK + whitespace only): skip the tokenizer entirely.
        return round(cjk_chars * settings.token_cjk_factor)
    return len(_get_encoding().encode(non_cjk)) + round(
        cjk_chars * settings.token_cjk_factor
    )


def count_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Count total tokens in a message list (OpenAI chat format).

    Accounts for per-message overhead and tool_calls content.
    """
    if not messages:
        return 0

    total = _REPLY_PRIMING
    for msg in messages:
        total += _MSG_OVERHEAD

        # Content field
        content = msg.get("content")
        if content and isinstance(content, str):
            total += count_tokens(content)

        # Tool calls (assistant messages)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                total += count_tokens(fn.get("name", ""))
                total += count_tokens(fn.get("arguments", ""))

        # Role token
        total += count_tokens(msg.get("role", ""))

    return total
