"""Desktop UI automation tools — inspect / click / type / hotkey (UIA).

Patterns in descriptions are suggestions, not fixed pipelines. Agent should
adapt order to the live UI (inspect → act → re-inspect as needed).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult
from agent_assistant.tools.sanitize import sanitize_error
from agent_assistant.tools.ui_driver import resolve_window, ui_driver

logger = logging.getLogger(__name__)


def ui_inspect_snapshot_key(raw_args: str) -> str:
    """Supersede scope for a ui_inspect call (declarative snapshot semantics).

    Unfiltered calls are keyed by WINDOW so a newer full tree stubs older
    full trees. Filtered calls (control_type / name_contains) get their own
    key — an empty ListItem scan must not wipe the last useful full tree
    (that caused coordinate-guessing on NetEase). Exposed module-level so
    memory-manager tests can inject identical semantics without the registry.
    """
    try:
        args = json.loads(raw_args or "{}")
        title = str(args.get("title_pattern") or "").strip().lower()
        ct = str(args.get("control_type") or "").strip().lower()
        nm = str(args.get("name_contains") or "").strip().lower()
    except Exception:
        return ""
    if ct or nm:
        return f"{title}|ct={ct}|n={nm}"
    return title


def _fail_from_exc(exc: BaseException) -> ToolResult:
    if isinstance(exc, LookupError):
        return ToolResult.failure(str(exc), code=404, error_category="not_found")
    if isinstance(exc, ValueError):
        return ToolResult.failure(str(exc), code=400, error_category="invalid_args")
    safe = sanitize_error(str(exc), context="ui_automation", code=500)
    logger.debug("ui automation error: %s", exc, exc_info=True)
    return ToolResult.failure(
        safe.safe_message,
        code=safe.code or 500,
        error_category=safe.category,
    )


class UiInspectTool(Tool):
    @property
    def name(self) -> str:
        return "ui_inspect"

    @property
    def is_snapshot(self) -> bool:
        # A control tree is a point-in-time measurement: a newer tree of the
        # same window/filter supersedes older ones (memory manager stubs them).
        return True

    @property
    def snapshot_keep_recent(self) -> int | None:
        # A song-search turn holds several distinct trees (搜索 / 夜曲 /
        # full); keep the 3 newest non-empty ones overall.
        return 3

    def snapshot_key(self, raw_args: str) -> str:
        return ui_inspect_snapshot_key(raw_args)

    @property
    def description(self) -> str:
        return (
            "Read a compact UI Automation control summary for a window "
            "(foreground by default, or match title_pattern). "
            "NEVER inspect AgentAssistant::UI unless the user asked to operate on "
            "this assistant. Players (网易云 etc.) use the current song as the "
            "window title — pass launch_app's window_title, not the product name. "
            "Always pass title_pattern once you know it; the assistant often stays "
            "foreground so omitting it inspects the wrong window. "
            "Each control: id (e.g. 'e12'), type, name, rect [x,y,w,h] relative "
            "to the window client area, parent (container name), row (index in parent). "
            "Results are cached ~30s: ui_click/ui_type target can be the id or exact "
            "name directly (fast, no re-scan). "
            "When to call: before interacting with an unfamiliar window; after the UI "
            "structure likely changed. "
            "Use name_contains to find a song/row (e.g. name_contains='夜曲') — "
            "that path is a UIA name search, not a shallow sidebar walk. "
            "control_type filters by type (ListItem, Edit, Document). Unfiltered "
            "inspect omits unnamed Group/Pane and Image chrome. "
            "Suggested pattern: launch_app or focus_window → ui_inspect → chain "
            "ui_click / ui_type / ui_hotkey (their results carry after.foreground) → "
            "re-inspect only when unsure. Not a fixed pipeline. "
            "When NOT to call: after every single click — action results already include "
            "after.foreground; do not re-inspect an unchanged window; "
            "do not invent clicks when the tree is empty — tell the user."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Optional window title substring; omit to use the foreground window",
                required=False,
            ),
            ToolParameter(
                name="max_depth",
                type="integer",
                description="Max tree depth to walk (default 4, max 8)",
                required=False,
                default=4,
            ),
            ToolParameter(
                name="max_nodes",
                type="integer",
                description="Max controls to return (default 80, max 200)",
                required=False,
                default=80,
            ),
            ToolParameter(
                name="control_type",
                type="string",
                description="Filter by control type substring (e.g. ListItem, Edit, Document)",
                required=False,
            ),
            ToolParameter(
                name="name_contains",
                type="string",
                description="Filter by name/automation_id substring (e.g. 夜曲)",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        try:
            win = resolve_window(kwargs.get("title_pattern"))
            max_depth = kwargs.get("max_depth", 4)
            max_nodes = kwargs.get("max_nodes", 80)
            nodes = ui_driver.inspect(
                win.hwnd,
                max_depth=int(max_depth) if max_depth is not None else 4,
                max_nodes=int(max_nodes) if max_nodes is not None else 80,
                control_type=kwargs.get("control_type"),
                name_contains=kwargs.get("name_contains"),
            )
            payload: dict[str, Any] = {
                "window": win.title,
                "pid": win.pid,
                "count": len(nodes),
                "controls": [n.to_dict() for n in nodes],
                "actionable_count": sum(1 for n in nodes if n.actionable),
            }
            skipped = getattr(ui_driver, "last_skipped_unnamed", 0) or 0
            if skipped:
                payload["skipped_unnamed"] = skipped
            hints: list[str] = []
            if kwargs.get("name_contains"):
                if nodes:
                    hints.append(
                        "Search-box Edit matching the query was omitted. "
                        "Remaining Text/ListItem rows are playable results — "
                        "ui_click(button=double, target=id). Do not ui_type again "
                        "if the query is already in the box."
                    )
                else:
                    hints.append(
                        "No matching song/row in the UI tree. Wait for results "
                        "to render, then ui_inspect(name_contains=关键词) again. "
                        "Do not click the search Edit."
                    )
            if any(n.control_type == "Document" for n in nodes):
                hints.append(
                    "Chromium/self-drawn UI: songs/rows often appear as Text, "
                    "not ListItem. ui_click target can be a Text name/id (uses rect) "
                    "even when actionable_count is 0. Do not click AgentAssistant."
                )
            if skipped:
                hints.append(
                    "Unnamed Group/Pane chrome was omitted so named controls fit. "
                    "To find a song/row, call ui_inspect with name_contains=关键词."
                )
            if hints:
                payload["hint"] = " ".join(hints)
            return ToolResult.success(data=payload)
        except Exception as exc:
            return _fail_from_exc(exc)


class UiClickTool(Tool):
    @property
    def name(self) -> str:
        return "ui_click"

    @property
    def requires_confirm(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Click a control in a desktop window by target (id like 'e12' from the last "
            "ui_inspect, control name, or automation_id) OR by coordinates relative to "
            "the window. Targets resolve against the last inspect's cache first (fast); "
            "cache miss falls back to a live search. "
            "The result includes after.foreground — use it to judge the "
            "outcome instead of re-inspecting immediately. "
            "When to call: ui_inspect already shows a matching clickable control, or you "
            "know where to click from inspect rects. "
            "Coordinate fallback: if the tree has no named target (e.g. self-drawn list), "
            "click a nearby Text node by name/id (rect is enough) rather than guessing "
            "x/y; or pass x/y relative to the window, or target+dx/dy. "
            "Always pass title_pattern — omitting it clicks the assistant chat. "
            "Suggested pattern: after search results appear, "
            "ui_click(button=double, target=the Text id of the song row) — "
            "do not click the search Edit just because it now has the same name. "
            "Chain confident sequences in ONE round. Not a fixed order — adapt to the app. "
            "When NOT to call: do not blind-click without a target or coordinates; "
            "opening apps → launch_app; focusing only → focus_window."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="target",
                type="string",
                description="Control name, automation_id, or path fragment from ui_inspect",
                required=False,
            ),
            ToolParameter(
                name="x",
                type="integer",
                description="X coordinate relative to window client area (omit if using target)",
                required=False,
            ),
            ToolParameter(
                name="y",
                type="integer",
                description="Y coordinate relative to window client area (omit if using target)",
                required=False,
            ),
            ToolParameter(
                name="dx",
                type="integer",
                description="Offset X from resolved control center or x (default 0)",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="dy",
                type="integer",
                description="Offset Y from resolved control center or y (default 0)",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Optional window title substring; omit to use foreground",
                required=False,
            ),
            ToolParameter(
                name="button",
                type="string",
                description="left | right | double (default left)",
                required=False,
                default="left",
                enum=["left", "right", "double"],
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        target = str(kwargs.get("target", "")).strip() or None
        x = kwargs.get("x")
        y = kwargs.get("y")
        dx = int(kwargs.get("dx", 0) or 0)
        dy = int(kwargs.get("dy", 0) or 0)
        if target is None and (x is None or y is None):
            return ToolResult.failure(
                "parameter 'target' or both 'x' and 'y' are required", code=400
            )
        try:
            win = resolve_window(kwargs.get("title_pattern"))
            data = ui_driver.click(
                win.hwnd,
                target,
                button=str(kwargs.get("button") or "left"),
                x=int(x) if x is not None else None,
                y=int(y) if y is not None else None,
                dx=dx,
                dy=dy,
            )
            data["window"] = win.title
            return ToolResult.success(data=data)
        except Exception as exc:
            return _fail_from_exc(exc)


class UiTypeTool(Tool):
    @property
    def name(self) -> str:
        return "ui_type"

    @property
    def requires_confirm(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Type literal text into the focused control, or into a target control "
            "(id like 'e12' from the last ui_inspect, name, or automation_id — resolved "
            "from the inspect cache first). Prefers a nearby Edit when the target is a "
            "label such as Text「搜索」, then UIA ValuePattern (via=value), then keyboard "
            "send_keys (via=keys). Does not use the clipboard. "
            "Result includes field_value, landed (true/false/null), after.foreground. "
            "When to call: an edit/search box is focused or you have a target name/id. "
            "Always pass title_pattern for the target app; never type into "
            "AgentAssistant::UI. "
            "Suggested pattern for in-app search (NetEase etc.): "
            "ui_hotkey ctrl+f (or ctrl+l) to focus the real search Edit, then "
            "ui_type with clear=true, then enter only if landed is not false. "
            "When NOT to call: sending only a shortcut → ui_hotkey; "
            "clicking a list item → ui_click."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="text",
                type="string",
                description="Text to type (literal; not a hotkey chord)",
            ),
            ToolParameter(
                name="target",
                type="string",
                description="Optional control name/id to focus before typing",
                required=False,
            ),
            ToolParameter(
                name="clear",
                type="boolean",
                description="If true, select-all + delete before typing (default false)",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Optional window title substring; omit to use foreground",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        if "text" not in kwargs or kwargs.get("text") is None:
            return ToolResult.failure("parameter 'text' is required", code=400)
        text = str(kwargs.get("text"))
        try:
            win = resolve_window(kwargs.get("title_pattern"))
            data = ui_driver.type_text(
                win.hwnd,
                text,
                clear=bool(kwargs.get("clear", False)),
                target=kwargs.get("target"),
            )
            data["window"] = win.title
            return ToolResult.success(data=data)
        except Exception as exc:
            return _fail_from_exc(exc)


class UiHotkeyTool(Tool):
    @property
    def name(self) -> str:
        return "ui_hotkey"

    @property
    def requires_confirm(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Send a keyboard shortcut to the target window (e.g. ctrl+f, enter, alt+tab is "
            "discouraged — prefer focusing the right window first). "
            "Result includes after.foreground. "
            "When to call: search, confirm, play/pause, or other actions that have a stable "
            "shortcut — often more reliable than clicking. "
            "Suggested pattern: focus/launch the app → optional ui_inspect → ui_hotkey; "
            "chain with ui_type for search boxes in one round. Not a fixed pipeline. "
            "When NOT to call: must click a specific list row with no shortcut → ui_click; "
            "opening an app → launch_app."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="keys",
                type="string",
                description="Chord like 'ctrl+f', 'enter', 'alt+enter', 'ctrl+shift+s'",
            ),
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Optional window title substring; omit to use foreground",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        keys = str(kwargs.get("keys", "")).strip()
        if not keys:
            return ToolResult.failure("parameter 'keys' is required", code=400)
        try:
            win = resolve_window(kwargs.get("title_pattern"))
            data = ui_driver.hotkey(win.hwnd, keys)
            data["window"] = win.title
            return ToolResult.success(data=data)
        except Exception as exc:
            return _fail_from_exc(exc)


class UiScrollTool(Tool):
    @property
    def name(self) -> str:
        return "ui_scroll"

    @property
    def requires_confirm(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Scroll a window or a specific scrollable control (list, panel, document). "
            "When to call: target content is below/above the visible area and the control tree "
            "does not show it (virtualized lists, long pages). "
            "Suggested pattern: after scrolling, call ui_inspect again to read the newly "
            "visible items before clicking. "
            "When NOT to call: the target is already visible in ui_inspect — just click it; "
            "PageUp/PageDown keys work → ui_hotkey."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="direction",
                type="string",
                description="Scroll direction: up | down | left | right",
                enum=["up", "down", "left", "right"],
            ),
            ToolParameter(
                name="amount",
                type="integer",
                description="Number of wheel clicks / lines to scroll (default 3, max 20)",
                required=False,
                default=3,
            ),
            ToolParameter(
                name="target",
                type="string",
                description="Optional control name to scroll within (default: window center)",
                required=False,
            ),
            ToolParameter(
                name="title_pattern",
                type="string",
                description="Optional window title substring; omit to use foreground",
                required=False,
            ),
        ]

    def execute(self, **kwargs: Any) -> ToolResult:
        direction = str(kwargs.get("direction", "down")).strip().lower()
        amount = int(kwargs.get("amount", 3) or 3)
        target = kwargs.get("target")
        try:
            win = resolve_window(kwargs.get("title_pattern"))
            data = ui_driver.scroll(
                win.hwnd,
                direction=direction,
                amount=amount,
                target=str(target).strip() if target else None,
            )
            data["window"] = win.title
            return ToolResult.success(data=data)
        except Exception as exc:
            return _fail_from_exc(exc)
