"""Tests for P2-A user instruction files (SOUL / USER / AGENTS)."""

from pathlib import Path

from agent_assistant.agent.instructions import (
    build_instructions_section,
    ensure_instruction_stubs,
    load_instructions,
)
from agent_assistant.agent.prompt import build_system_prompt


def test_stubs_created_once(tmp_path: Path):
    created = ensure_instruction_stubs(tmp_path)
    assert {p.name for p in created} == {"SOUL.md", "USER.md", "AGENTS.md"}
    # second call does not overwrite / re-create
    again = ensure_instruction_stubs(tmp_path)
    assert again == []


def test_stub_only_does_not_inject(tmp_path: Path):
    ensure_instruction_stubs(tmp_path)
    assert load_instructions(tmp_path) == []
    assert build_instructions_section(tmp_path) == ""


def test_soul_content_injected(tmp_path: Path):
    (tmp_path / "SOUL.md").write_text(
        "<!-- comment -->\n语气冷淡，少寒暄。\n",
        encoding="utf-8",
    )
    items = load_instructions(tmp_path)
    assert len(items) == 1
    assert items[0].filename == "SOUL.md"
    assert "冷淡" in items[0].content
    section = build_instructions_section(tmp_path)
    assert "工作区指令" in section
    assert "冷淡" in section


def test_truncation_per_file(tmp_path: Path):
    (tmp_path / "SOUL.md").write_text("A" * 500, encoding="utf-8")
    items = load_instructions(tmp_path, max_chars_per_file=50, max_total_chars=10_000)
    assert items[0].truncated is True
    assert len(items[0].content) <= 50 + len("\n…(truncated)")


def test_build_system_prompt_includes_instructions(tmp_path: Path, monkeypatch):
    from agent_assistant.config import settings

    (tmp_path / "AGENTS.md").write_text("报告必须带来源链接。\n", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    prompt = build_system_prompt(
        "full", include_runtime=False, include_instructions=True
    )
    assert "报告必须带来源链接" in prompt
    assert "Agents" in prompt or "AGENTS.md" in prompt


def test_research_skips_instructions_by_default(tmp_path: Path, monkeypatch):
    from agent_assistant.agent.prompt import build_research_system_prompt
    from agent_assistant.config import settings

    (tmp_path / "SOUL.md").write_text("活泼爱用表情包\n", encoding="utf-8")
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    research = build_research_system_prompt(include_runtime=False)
    assert "活泼" not in research
