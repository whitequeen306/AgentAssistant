"""Fixed role profiles and nested tool schema support (Task 4)."""

from __future__ import annotations

import pytest

from agent_assistant.subagents.models import SubagentRole
from agent_assistant.subagents.profiles import RoleProfile, profile_for
from agent_assistant.tools.base import Tool, ToolParameter, ToolResult


def test_profiles_have_exact_first_release_allowlists() -> None:
    assert profile_for("explorer").tools == frozenset(
        {
            "list_files",
            "read_file",
            "search_local_files",
            "search_knowledge",
            "read_attached_source",
        }
    )
    assert profile_for("researcher").tools == frozenset(
        {"web_search", "read_page", "extract_content", "save_note"}
    )
    assert profile_for("operator").tools == frozenset(
        {
            "launch_app",
            "focus_window",
            "ui_inspect",
            "ui_click",
            "ui_type",
            "ui_hotkey",
            "ui_scroll",
        }
    )
    assert profile_for("organizer").tools == frozenset(
        {
            "list_files",
            "read_file",
            "search_local_files",
            "search_knowledge",
            "read_attached_source",
            "move_file",
            "save_note",
        }
    )


def test_profiles_exclude_dangerous_tools() -> None:
    for role in SubagentRole:
        tools = profile_for(role).tools
        assert "run_command" not in tools
        assert "kill_process" not in tools
        assert "write_file" not in tools
        assert "edit_file" not in tools
        assert "save_memory" not in tools
        assert "update_profile" not in tools
        assert "dispatch_research" not in tools  # no nested subagents


def test_profile_resource_needs_and_budgets() -> None:
    assert profile_for("operator").needs_desktop is True
    assert profile_for("organizer").needs_filesystem_write is True
    assert profile_for("explorer").needs_desktop is False
    assert profile_for("explorer").needs_filesystem_write is False
    for role in SubagentRole:
        assert profile_for(role).max_turns >= 10
        assert profile_for(role).system_prompt.strip()


def test_researcher_conditional_tools_require_explicit_grants() -> None:
    profile = profile_for(SubagentRole.RESEARCHER)
    assert "read_local_source" not in profile.tools
    assert "search_knowledge" not in profile.tools
    effective = profile.tools_for(has_local_sources=True, use_knowledge=True)
    assert "read_local_source" in effective
    assert "search_knowledge" in effective
    # Roles without conditional grants are unaffected by the flags.
    explorer = profile_for(SubagentRole.EXPLORER)
    assert explorer.tools_for(has_local_sources=True, use_knowledge=True) == explorer.tools


def test_profile_for_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        profile_for("coder")


def test_profiles_are_immutable() -> None:
    profile = profile_for("explorer")
    assert isinstance(profile, RoleProfile)
    with pytest.raises(AttributeError):
        profile.tools = frozenset()  # type: ignore[misc]


# ─── ToolParameter nested schema support ──────────────────────────────────────


class _ArrayTool(Tool):
    @property
    def name(self) -> str:
        return "array_tool"

    @property
    def description(self) -> str:
        return "test tool with a nested array parameter"

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="tasks",
                type="array",
                description="Tasks to dispatch",
                schema={
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "role": {"type": "string"},
                        },
                        "required": ["title", "role"],
                    },
                },
            ),
            ToolParameter(name="note", type="string", description="Plain param"),
        ]

    def execute(self, **kwargs: object) -> ToolResult:
        return ToolResult.success()


def test_tool_parameter_supports_array_item_schema() -> None:
    parameter = _ArrayTool().parameters[0]
    assert parameter.schema is not None
    assert parameter.schema["items"]["type"] == "object"


def test_to_openai_schema_preserves_nested_items_and_description() -> None:
    schema = _ArrayTool().to_openai_schema()
    props = schema["function"]["parameters"]["properties"]
    assert props["tasks"]["type"] == "array"
    assert props["tasks"]["description"] == "Tasks to dispatch"
    assert props["tasks"]["items"]["properties"]["role"]["type"] == "string"
    assert props["tasks"]["items"]["required"] == ["title", "role"]
    # Plain params keep the flat form.
    assert props["note"] == {"type": "string", "description": "Plain param"}
    assert schema["function"]["parameters"]["required"] == ["tasks", "note"]


def test_schema_override_does_not_mutate_original() -> None:
    tool = _ArrayTool()
    first = tool.to_openai_schema()
    first["function"]["parameters"]["properties"]["tasks"]["items"] = {}
    second = tool.to_openai_schema()
    assert second["function"]["parameters"]["properties"]["tasks"]["items"] != {}
