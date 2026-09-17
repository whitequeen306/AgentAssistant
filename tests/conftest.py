"""Shared test fixtures."""

from __future__ import annotations

import pytest

from agent_assistant.memory.session_trace import SessionTrace
from agent_assistant.tools.tool_archive import ToolArchive


@pytest.fixture(autouse=True)
def _tmp_tool_archive(monkeypatch: pytest.MonkeyPatch, tmp_path) -> ToolArchive:
    """Redirect the tool archive to a temp dir for EVERY test.

    The agent loop archives every tool result; without this, any test that
    drives the loop would append records into the real
    ``~/AgentAssistant/sessions/<conv>/tool_details.jsonl``. Both import
    sites are patched, AND the live singleton is repointed — a test that
    captured the singleton earlier (direct import) would otherwise still
    write to the real directory.
    """
    from agent_assistant.tools import tool_archive as ta_mod

    archive = ToolArchive(base_dir=tmp_path / "tool_archive")
    # Capture the live singleton BEFORE the module attribute is swapped.
    original = ta_mod.tool_archive
    monkeypatch.setattr(original, "_base_dir", tmp_path / "tool_archive")
    monkeypatch.setattr(
        original._trace, "_base_dir", tmp_path / "tool_archive"
    )
    monkeypatch.setattr(ta_mod, "tool_archive", archive)
    monkeypatch.setattr("agent_assistant.agent.loop.tool_archive", archive)
    return archive


@pytest.fixture(autouse=True)
def _tmp_session_trace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> SessionTrace:
    """Redirect the session event stream to a temp dir for EVERY test.

    Same reason as the archive above: driving the loop emits turn/user/llm/
    tool events, and a bare ``AgentLoop()`` in a test must not append them to
    the real ``~/AgentAssistant/sessions``.

    Two layers on purpose. Patching the module *attribute* only helps code
    that looks the name up at call time; anything holding a reference to the
    singleton itself (``from ... import session_trace``) would keep writing to
    the real directory — which is exactly how test runs leaked files into
    ``~/AgentAssistant/sessions``. So the singleton's own ``_base_dir`` is
    repointed too.
    """
    from agent_assistant.memory import session_trace as st_mod

    trace = SessionTrace(base_dir=tmp_path / "sessions")
    # Layer 2 first: repoint the live singleton (safe for stale references).
    monkeypatch.setattr(st_mod.session_trace, "_base_dir", tmp_path / "sessions")
    # Layer 1: swap the module-level name the loop reads at call time.
    monkeypatch.setattr(st_mod, "session_trace", trace)
    monkeypatch.setattr("agent_assistant.agent.loop.session_trace", trace)
    return trace
