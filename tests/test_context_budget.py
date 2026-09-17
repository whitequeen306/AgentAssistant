"""上下文预算的算术：消息预算 + 固定开销必须装得进模型窗口。

背景（2026-09-14 实测发现的缺陷）：``effective_memory_budget`` 曾被直接设为
``llm_context_window × compaction_ratio``（128K × 0.85 = 108,800），但那个数是
**整个请求**的预算——窗口还要装系统提示词（约 2.7K）和 38 个工具的 schema
（约 8.7K）。于是最坏情况 108,800 + 11,410 = 120,210（93.9%），再加上压缩的
10% 缓冲带就是 131,090（**102.4%**）——从"该压缩了"变成"直接溢出"。

这组测试锁住账目：任何一项变了都得重新核对，别再让它悄悄超窗。
"""

from __future__ import annotations

import json

import pytest

from agent_assistant.config import settings
from agent_assistant.memory.token_counter import count_tokens


def _fixed_overhead() -> int:
    """系统提示词 + 工具 schema 的实际占用。"""
    from agent_assistant.agent.prompt import build_system_prompt
    from agent_assistant.tools.register import register_all_tools
    from agent_assistant.tools.registry import tool_registry

    register_all_tools()
    return count_tokens(build_system_prompt("full")) + count_tokens(
        json.dumps(tool_registry.openai_schemas(), ensure_ascii=False)
    )


class TestBudgetAccounting:
    def test_budget_subtracts_fixed_overhead(self) -> None:
        """消息预算 = 85% 包络 − 固定开销预留，不是整个包络。"""
        envelope = int(settings.llm_context_window * settings.compaction_ratio)
        assert settings.effective_memory_budget == (
            envelope - settings.memory_context_reserve
        )
        assert settings.effective_memory_budget < envelope, (
            "预算等于包络时，系统提示词与工具 schema 就只能去挤模型窗口了"
        )

    def test_explicit_budget_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hosted 模式按请求钉死预算时必须原样生效（不扣预留）。"""
        monkeypatch.setattr(settings, "memory_token_budget", 5000)
        assert settings.effective_memory_budget == 5000

    def test_floor_protects_tiny_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """窗口极小时仍给出可用预算（而不是负数）。"""
        monkeypatch.setattr(settings, "llm_context_window", 8000)
        assert settings.effective_memory_budget >= 2000

    def test_worst_case_fits_the_window(self) -> None:
        """回归锁：压缩触发点（含缓冲带）必须仍装进窗口，且留得出输出空间。

        触发点是 conversation 达到 ``budget + margin`` 的那一刻，此时请求总量
        为 budget + margin + 固定开销。
        """
        window = settings.llm_context_window
        budget = settings.effective_memory_budget
        fixed = _fixed_overhead()
        # 压缩的防抖动缓冲带（manager 里的 margin = max(400, budget // 10)）
        margin = max(400, budget // 10)

        worst = budget + margin + fixed
        assert worst <= window, (
            f"压缩触发点 {worst:,} 已超出窗口 {window:,}："
            f"budget={budget:,} margin={margin:,} fixed={fixed:,}"
        )
        # DeepSeek 单次输出上限约 8K —— 必须留得出来，否则模型写不完回复。
        assert window - worst >= 8000, (
            f"只剩 {window - worst:,} tokens 给输出，不够写完整回复"
        )
