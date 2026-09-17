"""Spec tests for the main agent system prompt persona.

Locks the mature-companion voice so the prompt can't silently regress to a
sycophantic / emoji-heavy / cold-machine style. This is a content spec, not
logic — the assertions encode the user's explicit tone requirements.
"""

from agent_assistant.agent.prompt import (
    SYSTEM_PROMPT,
    build_research_system_prompt,
    build_runtime_section,
    build_system_prompt,
)


def test_prompt_establishes_companion_persona():
    # 陪伴型: a long-term partner, not a one-shot Q&A bot
    assert "陪伴" in SYSTEM_PROMPT


def test_prompt_requires_mature_restrained_voice():
    # 成熟/克制 — not childish, not effusive
    assert "成熟" in SYSTEM_PROMPT or "克制" in SYSTEM_PROMPT


def test_prompt_bans_emoji_stickers():
    # The #1 user complaint: emoji-heavy childish responses
    assert "表情包" in SYSTEM_PROMPT


def test_prompt_bans_sycophantic_filler():
    # No "好的！"/"当然可以！"/"好问题！" padding + general anti-sycophancy terms
    assert "好的" in SYSTEM_PROMPT or "当然可以" in SYSTEM_PROMPT
    assert "阿谀" in SYSTEM_PROMPT or "附和" in SYSTEM_PROMPT


def test_prompt_has_warmth_not_cold_machine():
    # Companion has 温度, not a cold emotionless tool
    assert "温度" in SYSTEM_PROMPT or "关心" in SYSTEM_PROMPT


def test_prompt_keeps_agentic_and_safety():
    # Core capabilities + safety must remain after the rewrite
    assert "Agentic" in SYSTEM_PROMPT or "agentic" in SYSTEM_PROMPT.lower()
    assert "危险" in SYSTEM_PROMPT or "确认" in SYSTEM_PROMPT


def test_prompt_matches_user_language():
    assert "语言" in SYSTEM_PROMPT


def test_prompt_defaults_to_chinese_except_technical_terms():
    # 用户明确要求：默认用中文回复，仅技术术语（API 名、代码标识符等）保留原文。
    # 旧 prompt 只写"用用户的语言回复"——不够，必须显式默认中文 + 技术术语例外。
    assert "中文" in SYSTEM_PROMPT
    assert "技术术语" in SYSTEM_PROMPT


def test_prompt_preserves_capability_list():
    # Old prompt listed translate/summarize/answer/write/reason/plan — must remain
    for cap in ("翻译", "摘要", "问答", "写作", "推理", "规划"):
        assert cap in SYSTEM_PROMPT, f"capability {cap} missing from prompt"


def test_prompt_exfiltration_safety_clause():
    # Desktop agent with file+web access must forbid data exfiltration, not just destruction
    assert "外发" in SYSTEM_PROMPT or "外泄" in SYSTEM_PROMPT or "上传" in SYSTEM_PROMPT


def test_prompt_emoji_exception_excludes_tool_content():
    # Prevent prompt-injection via fetched web/file content masquerading as "user style"
    assert "工具返回" in SYSTEM_PROMPT or "工具内容" in SYSTEM_PROMPT


def test_prompt_includes_study_task_habits():
    # Habits section is now study-scoped (learning loop), not generic life tasks.
    assert "常见学习任务" in SYSTEM_PROMPT
    assert "学习闭环" in SYSTEM_PROMPT
    assert "web_search" in SYSTEM_PROMPT
    assert "save_note" in SYSTEM_PROMPT
    assert "list_files" in SYSTEM_PROMPT
    assert "read_file" in SYSTEM_PROMPT
    # Habits are guidance, not a hardcoded pipeline
    assert "非固定流程" in SYSTEM_PROMPT


def test_prompt_identity_is_study_agent_not_desktop_helper():
    # Product positioning: 大学生自主学习智能体, not a generic desktop assistant.
    assert "大学生自主学习智能体" in SYSTEM_PROMPT
    assert "桌面助手" not in SYSTEM_PROMPT
    # The learning loop must be named end-to-end in the prompt.
    for stage in ("精读", "调研", "沉淀", "自测", "复习"):
        assert stage in SYSTEM_PROMPT, f"learning-loop stage {stage} missing"
    # Study data stays local.
    assert "不出本机" in SYSTEM_PROMPT


def test_prompt_describes_subagent_orchestration():
    # Four fixed personal-assistant roles, delegation criteria, and the
    # await-once / no-polling rule.
    for role in ("explorer", "researcher", "operator", "organizer"):
        assert role in SYSTEM_PROMPT, f"role {role} missing from prompt"
    assert "dispatch_subagents" in SYSTEM_PROMPT
    assert "await_subagents" in SYSTEM_PROMPT
    assert "control_subagent" in SYSTEM_PROMPT
    assert "禁止轮询" in SYSTEM_PROMPT
    # No Coder role; no nested subagents.
    assert "没有 Coder" in SYSTEM_PROMPT
    assert "不能再派子任务" in SYSTEM_PROMPT
    # Memory/profile decisions stay with the main agent.
    assert "子任务不写记忆" in SYSTEM_PROMPT
    # Recovery stays autonomous.
    assert "禁止把恢复决策抛给用户" in SYSTEM_PROMPT
    # Delegation is for substantial parallel work, not trivial single steps.
    assert "琐碎单步" in SYSTEM_PROMPT
    # Legacy deep-research path remains documented.
    assert "dispatch_research" in SYSTEM_PROMPT


def test_build_full_matches_system_prompt_constant():
    # SYSTEM_PROMPT is the stable persona stack (no volatile Runtime / instructions)
    assert (
        build_system_prompt(
            "full", include_runtime=False, include_instructions=False
        )
        == SYSTEM_PROMPT
    )


def test_minimal_omits_companion_style():
    minimal = build_system_prompt(
        "minimal", include_runtime=False, include_instructions=False
    )
    assert "Agentic" in minimal or "agentic" in minimal.lower()
    assert "危险" in minimal or "确认" in minimal
    # Style / warmth guidance is full-mode only
    assert "表情包" not in minimal
    assert "阿谀" not in minimal
    assert "陪伴" in minimal  # identity still present


def test_runtime_section_injected_by_default():
    full = build_system_prompt("full")
    assert "# Runtime" in full
    assert "工作目录" in full or "CWD" in full
    rt = build_runtime_section(lang="zh")
    assert "操作系统" in rt
    assert "本地日期" in rt


def test_research_prompt_is_task_focused():
    research = build_research_system_prompt(include_runtime=False)
    assert "deep-research" in research.lower() or "research sub-agent" in research.lower()
    assert "Quality Bar" in research
    assert "web_search" in research
    # No Chinese companion persona
    assert "陪伴" not in research
    assert "表情包" not in research


def test_research_prompt_includes_english_runtime():
    research = build_research_system_prompt()
    assert "# Runtime" in research
    assert "Local date" in research or "CWD" in research


def test_research_prompt_accepts_extra():
    research = build_research_system_prompt(
        extra="## Background Context\nfoo",
        include_runtime=False,
    )
    assert "## Background Context" in research
    assert "foo" in research
