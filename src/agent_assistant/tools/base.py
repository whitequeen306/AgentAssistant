"""Base tool class and structured result type.

Design principles (from docs/02-philosophy.md):
- Error bubbling: tools return structured errors, never swallow them
- Side-effect encapsulation: internal side effects hidden from the model
- Idempotency preferred: safe to retry
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolResult:
    """Structured result returned by every tool call.

    The model sees `to_model_str()` — a JSON string with ok/data/error.
    Failures also carry ``error_category`` + compact ``feedback.trace``.
    """

    ok: bool
    data: Any = None
    error: str | None = None
    code: int | None = None
    warning: str | None = None
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            d["data"] = self.data
        if self.error is not None:
            d["error"] = self.error
        if self.code is not None:
            d["code"] = self.code
        if self.warning is not None:
            d["warning"] = self.warning
        if not self.ok:
            from agent_assistant.tools.failure_feedback import classify_tool_error

            d["error_category"] = self.error_category or classify_tool_error(
                self.error, self.code
            )
        return d

    def to_model_str(
        self,
        turn_trace: list[dict[str, Any]] | None = None,
    ) -> str:
        """Serialize to JSON string for the model to read.

        On failure, includes ``feedback.hint`` + recent failure ``trace`` so the
        next agent round can change strategy automatically.
        """
        if self.ok:
            return json.dumps(self.to_dict(), ensure_ascii=False)
        from agent_assistant.tools.failure_feedback import build_failure_payload

        payload = build_failure_payload(
            error=self.error,
            code=self.code,
            error_category=self.error_category,
            data=self.data,
            turn_trace=turn_trace,
        )
        if self.warning is not None:
            payload["warning"] = self.warning
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def success(cls, data: Any = None, warning: str | None = None) -> ToolResult:
        return cls(ok=True, data=data, warning=warning)

    @classmethod
    def failure(
        cls,
        error: str,
        code: int | None = None,
        error_category: str | None = None,
    ) -> ToolResult:
        return cls(ok=False, error=error, code=code, error_category=error_category)


@dataclass
class ToolParameter:
    """Describes a single tool parameter for OpenAI function schema."""

    name: str
    type: str  # "string" | "integer" | "number" | "boolean" | "array" | "object"
    description: str
    required: bool = True
    enum: list[str] | None = None
    default: Any = None
    # Full JSON-schema fragment for nested array/object parameters. When set,
    # it is used as the base schema (items/properties preserved) and only the
    # description is overlaid.
    schema: dict[str, Any] | None = None


class Tool(ABC):
    """Abstract base class for all agent tools.

    Subclasses must define:
        name, description, parameters, execute()
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (model calls this identifier)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """What it does / when to call / when NOT to call. Model routes by this."""
        ...

    @property
    def requires_confirm(self) -> bool:
        """If True, permission policy falls back to ASK when no rule matches.

        Prefer adding a PermissionRule in ``permission.default_permission_policy``.
        Keep this True on inherently dangerous tools as a safety net.
        """
        return False

    @property
    def is_snapshot(self) -> bool:
        """True when results are point-in-time measurements of mutable state.

        A later call with the same ``snapshot_key`` supersedes older results,
        so the memory manager stubs the older payloads to save context.
        Declare this ONLY on tools that re-measure the same subject (UI
        trees, perf readings); never on tools whose results have independent
        value (searches, page bodies, file reads).
        """
        return False

    @property
    def snapshot_keep_recent(self) -> int | None:
        """Cap non-empty snapshots kept per tool beyond supersession.

        None = supersession only (a newer same-key result stubs older ones).
        An integer N keeps the N newest non-empty results overall — useful
        when one turn legitimately gathers several distinct snapshots.
        """
        return None

    def snapshot_key(self, raw_args: str) -> str:
        """Supersede scope key for one call (default: exact argument string)."""
        return raw_args or ""

    @property
    @abstractmethod
    def parameters(self) -> list[ToolParameter]:
        """Parameter definitions."""
        ...

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Run the tool. Must NEVER raise — return ToolResult.failure() instead."""
        ...

    def to_openai_schema(self) -> dict[str, Any]:
        """Generate OpenAI function-calling tool schema."""
        properties: dict[str, Any] = {}
        required: list[str] = []

        for p in self.parameters:
            if p.schema is not None:
                # Nested schema wins; keep items/properties, overlay description.
                prop: dict[str, Any] = dict(p.schema)
                prop.setdefault("type", p.type)
                prop["description"] = p.description
            else:
                prop = {
                    "type": p.type,
                    "description": p.description,
                }
            if p.enum:
                prop["enum"] = p.enum
            if p.default is not None:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }
