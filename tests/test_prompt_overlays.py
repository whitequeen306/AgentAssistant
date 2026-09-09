"""P3: model-family prompt overlays (DeepSeek text tuning)."""

from agent_assistant.agent.prompt import (
    SYSTEM_PROMPT,
    build_research_system_prompt,
    build_system_prompt,
)
from agent_assistant.agent.prompt_overlays import (
    resolve_prompt_family,
    overlay_for_main,
)


def test_resolve_deepseek_family():
    assert resolve_prompt_family("deepseek-v4-flash") == "deepseek"
    assert resolve_prompt_family("deepseek-chat") == "deepseek"
    assert resolve_prompt_family("gpt-4o") == "default"


def test_deepseek_overlay_in_default_system_prompt():
    assert "模型侧执行偏好（DeepSeek）" in SYSTEM_PROMPT
    assert "真正发出对应 tool call" in SYSTEM_PROMPT


def test_non_deepseek_skips_overlay():
    prompt = build_system_prompt(
        "full",
        include_runtime=False,
        include_instructions=False,
        include_model_overlay=True,
        model="gpt-4o",
    )
    assert "模型侧执行偏好（DeepSeek）" not in prompt
    assert overlay_for_main("default") == ""


def test_research_deepseek_overlay():
    research = build_research_system_prompt(
        include_runtime=False,
        include_model_overlay=True,
        model="deepseek-v4-flash",
    )
    assert "Model bias (DeepSeek)" in research
