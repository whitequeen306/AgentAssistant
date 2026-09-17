"""Model-family prompt overlays (P3) — text tuning, not fine-tuning.

OpenCode-style: same product sections + a short family-specific bias block.
Currently only DeepSeek is specialized; other models get no overlay.
"""

from __future__ import annotations

from typing import Literal

PromptFamily = Literal["deepseek", "default"]

# Compact bias for DeepSeek chat/tool models (V3 / R1-distill / flash, etc.).
# Written for the model, not end users.
SECTION_DEEPSEEK_MAIN = """\
# 模型侧执行偏好（DeepSeek）
- 任务需要工具时：优先发出 tool call，少写长规划、少复述用户原话。
- 说「去搜 / 去读 / 去整理」时，同一轮必须真正发出对应 tool call，禁止只口头承诺。
- 彼此独立的工具调用可并行；有先后依赖则串行。
- 日常学习请求（查资料、读网页、读文件、整理资料、启动学习相关应用）默认执行；安全由框架权限策略拦截，不要自行过度拒答。
- 对用户用中文短句给结论；工具参数只填 schema 字段，不要把解释塞进参数字符串。
- 工具失败：根据返回的 error_category / feedback 换策略再试，不要重复同一无效调用。
"""

SECTION_DEEPSEEK_RESEARCH = """\
# Model bias (DeepSeek)
- Prefer tool calls over long plans; emit search/read calls in the same turn you decide them.
- Independent web_search / read_page calls may run in parallel.
- Do not refuse ordinary research tasks; never invent URLs or citations.
- On weak results, change query or page — do not repeat the same failed call.
- Final report in clear Markdown; keep prose tight.
"""


def resolve_prompt_family(model: str | None = None) -> PromptFamily:
    """Map model id → prompt family. Defaults to configured deepseek_model."""
    if model is None:
        from agent_assistant.config import settings

        model = settings.deepseek_model
    mid = (model or "").lower()
    if "deepseek" in mid:
        return "deepseek"
    return "default"


def overlay_for_main(family: PromptFamily) -> str:
    if family == "deepseek":
        return SECTION_DEEPSEEK_MAIN
    return ""


def overlay_for_research(family: PromptFamily) -> str:
    if family == "deepseek":
        return SECTION_DEEPSEEK_RESEARCH
    return ""
