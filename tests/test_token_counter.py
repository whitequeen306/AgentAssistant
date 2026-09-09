"""CJK-calibrated token estimation for DeepSeek budget management.

cl100k_base overcounts Chinese (~0.7-1.0 tok/char) vs DeepSeek's documented
~0.6 tok/char — these tests pin the calibration: CJK text counted by factor,
non-CJK by tiktoken, mixed by both.
"""

from __future__ import annotations

import pytest

from agent_assistant.config import settings
from agent_assistant.memory.token_counter import count_tokens


class TestCjkCalibration:
    def test_pure_cjk_uses_factor(self):
        text = "打开网易云音乐播放一首歌" * 100  # 1200 CJK chars
        expected = round(len(text) * settings.token_cjk_factor)
        assert count_tokens(text) == expected

    def test_cjk_undershoots_cl100k(self):
        """The whole point: CJK text must count BELOW cl100k's raw count."""
        import tiktoken

        text = "今天帮用户整理了下载目录里的发票和合同文件" * 50
        raw_cl100k = len(tiktoken.get_encoding("cl100k_base").encode(text))
        assert count_tokens(text) < raw_cl100k

    def test_english_unchanged(self):
        import tiktoken

        text = "The quick brown fox jumps over the lazy dog. " * 20
        raw = len(tiktoken.get_encoding("cl100k_base").encode(text))
        assert count_tokens(text) == raw

    def test_mixed_text_counts_both_parts(self):
        cjk = "中文部分" * 100  # 400 chars
        eng = "english words " * 100
        # implementation: tiktoken(eng part) + factor(cjk chars) → exact sum
        assert count_tokens(cjk + eng) == count_tokens(cjk) + count_tokens(eng)

    def test_cjk_punctuation_and_fullwidth_included(self):
        text = "，。！？：（）" * 100
        assert count_tokens(text) == round(len(text) * settings.token_cjk_factor)

    def test_factor_configurable(self, monkeypatch: pytest.MonkeyPatch):
        text = "一二三四五"  # exactly 5 chars
        monkeypatch.setattr(settings, "token_cjk_factor", 1.0)
        assert count_tokens(text) == 5
        monkeypatch.setattr(settings, "token_cjk_factor", 0.5)
        assert count_tokens(text) == round(5 * 0.5)

    def test_empty_and_whitespace(self):
        assert count_tokens("") == 0
        assert count_tokens("   ") > 0  # whitespace still tokenized
