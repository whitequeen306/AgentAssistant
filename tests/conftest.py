"""Shared test fixtures."""

from __future__ import annotations

import pytest

from agent_assistant.tools.tool_archive import ToolArchive


@pytest.fixture(autouse=True)
def _tmp_tool_archive(monkeypatch: pytest.MonkeyPatch, tmp_path) -> ToolArchive:
    """Redirect the tool archive to a temp dir for EVERY test.

    The agent loop archives every tool result; without this, any test that
    drives the loop would append records into the real
    ``~/AgentAssistant/tool_archive``. Both import sites are patched: the
    loop's imported name and the module singleton used by the recall tool.
    """
    archive = ToolArchive(base_dir=tmp_path / "tool_archive")
    monkeypatch.setattr("agent_assistant.agent.loop.tool_archive", archive)
    monkeypatch.setattr(
        "agent_assistant.tools.tool_archive.tool_archive", archive
    )
    return archive
