"""Failure feedback: error_category + compact turn trace for the model."""

from __future__ import annotations

import json

from agent_assistant.tools.base import ToolResult
from agent_assistant.tools.failure_feedback import (
    classify_tool_error,
    make_failure_entry,
)


def test_classify_empty_html():
    assert classify_tool_error("page returned empty HTML (blocked)") == "empty_html"


def test_classify_confirm_denied():
    assert (
        classify_tool_error("User denied confirmation for 'save_note'.")
        == "confirm_denied"
    )


def test_failure_model_str_includes_feedback_trace():
    r = ToolResult.failure(
        "page returned empty HTML",
        code=422,
        error_category="empty_html",
    )
    trace = [
        make_failure_entry(
            "read_page",
            error="page returned empty HTML",
            code=422,
            error_category="empty_html",
            arguments='{"url":"https://example.com/a"}',
        )
    ]
    payload = json.loads(r.to_model_str(turn_trace=trace))
    assert payload["ok"] is False
    assert payload["error_category"] == "empty_html"
    assert "hint" in payload["feedback"]
    assert payload["feedback"]["trace"][0]["args"]["host"] == "example.com"
    assert "Do NOT retry" in payload["feedback"]["hint"]


def test_success_model_str_unchanged_shape():
    r = ToolResult.success(data={"x": 1})
    payload = json.loads(r.to_model_str())
    assert payload == {"ok": True, "data": {"x": 1}}
