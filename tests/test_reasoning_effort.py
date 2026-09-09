"""Thinking intensity (REASONING_EFFORT) maps onto DeepSeek request kwargs."""

from __future__ import annotations

import pytest

from agent_assistant.config import apply_reasoning_effort, settings
from agent_assistant.llm.client import LLMClient


@pytest.fixture()
def restore_reasoning():
    prev_on = settings.thinking_enabled
    prev_effort = settings.reasoning_effort
    try:
        yield
    finally:
        settings.thinking_enabled = prev_on
        settings.reasoning_effort = prev_effort


def test_apply_off_disables_thinking(restore_reasoning):
    assert apply_reasoning_effort("off") is True
    assert settings.thinking_enabled is False
    assert settings.reasoning_effort == "off"


def test_apply_low_enables_thinking(restore_reasoning):
    apply_reasoning_effort("off")
    assert apply_reasoning_effort("low") is True
    assert settings.thinking_enabled is True
    assert settings.reasoning_effort == "low"


def test_apply_is_case_insensitive(restore_reasoning):
    assert apply_reasoning_effort("HIGH") is True
    assert settings.reasoning_effort == "high"
    assert settings.thinking_enabled is True


def test_apply_rejects_invalid_without_mutate(restore_reasoning):
    apply_reasoning_effort("low")
    assert apply_reasoning_effort("nope") is False
    assert settings.reasoning_effort == "low"
    assert settings.thinking_enabled is True


def test_llm_kwargs_include_low_effort(restore_reasoning):
    apply_reasoning_effort("low")
    kwargs = LLMClient()._build_kwargs([], None, 0.7, False)
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}


def test_llm_kwargs_disable_thinking_when_off(restore_reasoning):
    apply_reasoning_effort("off")
    kwargs = LLMClient()._build_kwargs([], None, 0.7, False)
    assert "reasoning_effort" not in kwargs
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
