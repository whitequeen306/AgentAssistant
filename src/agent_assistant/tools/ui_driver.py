"""Windows UI Automation driver (pywinauto UIA backend).

Used by ui_inspect / ui_click / ui_type / ui_hotkey. Keeps Win32/pywinauto
details out of the Tool classes so unit tests can mock this module.

Performance notes (2026-08 slow-run fix):
- Every cross-process UIA property read is a COM round trip (1-10ms); Chromium
  apps (NetEase cloud music etc.) expose thousands of nodes. The old inspect
  read each node's type/name twice, probed visibility twice, and re-enumerated
  the parent's children per matched node (O(n^2)) — 8-24s per call observed.
  Now each node's identity is read exactly once and visibility comes from the
  single rectangle read.
- find_control used descendants() (full-tree FindAll) — seconds on big apps.
  Now: inspect results are cached ~30s per window; ui_click/ui_type targets
  resolve from that cache first (zero COM) and fall back to a bounded BFS.
- Unfiltered inspect stays a shallow DFS (sidebar chrome is left-to-right at
  depth 4). ``name_contains`` must NOT use that walk — song rows in Chromium
  players sit deeper than the cap, so DFS returned n=0 and the model clicked
  the search Edit. Filtered inspect uses UIA FindAll by Name / Text / ListItem.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterator

from agent_assistant.win_utils import (
    WindowInfo,
    find_windows,
    find_windows_by_exe,
    focus_window_hwnd,
    is_assistant_window,
)

logger = logging.getLogger(__name__)

# Connection wrapper reuse window (Application.connect per call is wasteful)
_CONNECT_TTL_S = 20.0
# ui_click/ui_type may resolve targets from the last inspect for this long
_INSPECT_CACHE_TTL_S = 30.0
# Bounded live search (fallback when the cache misses)
_FIND_MAX_DEPTH = 12
_FIND_MAX_NODES = 800
# Hard cap on nodes VISITED per inspect (matched or not) — bounds worst case
_INSPECT_MAX_VISITS = 1500
# Name hunt may walk past sidebar chrome when FindAll is unavailable
_INSPECT_MAX_VISITS_NAMED = 8000
_FINDALL_CAP = 150
_FINDALL_SCAN_CAP = 4000
_NAME_SEARCH_TYPES = ("Text", "ListItem", "Hyperlink")
_NAME_RETRY_S = 0.4
# Chromium search overlays need a beat after click before keys/ValuePattern land
_TYPE_FOCUS_S = 0.08
_EDITABLE_TYPES = ("edit", "document", "combobox")
_RESULT_TYPES = ("text", "listitem", "hyperlink")

# Named keys → pywinauto send_keys tokens
_NAMED_KEYS: dict[str, str] = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "space": "{SPACE}",
    "backspace": "{BACKSPACE}",
    "bs": "{BACKSPACE}",
    "delete": "{DELETE}",
    "del": "{DELETE}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "home": "{HOME}",
    "end": "{END}",
    "pageup": "{PGUP}",
    "pagedown": "{PGDN}",
    "insert": "{INSERT}",
    "f1": "{F1}",
    "f2": "{F2}",
    "f3": "{F3}",
    "f4": "{F4}",
    "f5": "{F5}",
    "f6": "{F6}",
    "f7": "{F7}",
    "f8": "{F8}",
    "f9": "{F9}",
    "f10": "{F10}",
    "f11": "{F11}",
    "f12": "{F12}",
}


@dataclass
class ControlSummary:
    """One node in the truncated control-tree summary."""

    control_type: str
    name: str
    automation_id: str
    path: str
    enabled: bool
    actionable: bool  # clickable or editable heuristic
    rect: tuple[int, int, int, int] | None = None  # x, y, w, h relative to window
    uid: str = ""  # short id ("e12") — valid as ui_click/ui_type target while cached
    parent_name: str | None = None  # immediate container name (locality hint)
    row_index: int | None = None  # index within parent (for list items)

    def to_dict(self) -> dict[str, Any]:
        """Compact model-facing payload — every field here costs tokens on
        EVERY later round, so optional fields are omitted when empty."""
        d: dict[str, Any] = {
            "type": self.control_type,
            "name": self.name,
        }
        if self.uid:
            d["id"] = self.uid
        if self.automation_id:
            d["automation_id"] = self.automation_id
        if self.rect:
            d["rect"] = [self.rect[0], self.rect[1], self.rect[2], self.rect[3]]
        if self.parent_name:
            d["parent"] = self.parent_name
        if self.row_index is not None:
            d["row"] = self.row_index
        if not self.enabled:
            d["enabled"] = False
        if self.actionable:
            d["actionable"] = True
        return d


def parse_hotkey(keys: str) -> str:
    """Convert human hotkey like ``ctrl+f`` / ``alt+enter`` to pywinauto send_keys.

    Raises ValueError on empty / unsupported input.
    """
    raw = (keys or "").strip()
    if not raw:
        raise ValueError("keys is empty")

    # Already a send_keys token (e.g. "{ENTER}" or "^f")
    if raw.startswith("{") or raw.startswith(("^", "%", "+")):
        return raw

    parts = [p.strip().lower() for p in re.split(r"[+\-]", raw) if p.strip()]
    if not parts:
        raise ValueError("keys is empty")

    mods: list[str] = []
    main: str | None = None
    for p in parts:
        if p in ("ctrl", "control", "ctl"):
            mods.append("^")
        elif p in ("alt",):
            mods.append("%")
        elif p in ("shift",):
            mods.append("+")
        elif p in ("win", "windows", "meta", "cmd"):
            # pywinauto has no reliable Win modifier; reject clearly
            raise ValueError("Win/meta modifier is not supported")
        else:
            if main is not None:
                raise ValueError(f"multiple main keys in '{keys}'")
            main = p

    if main is None:
        raise ValueError(f"no main key in '{keys}'")

    if main in _NAMED_KEYS:
        token = _NAMED_KEYS[main]
    elif len(main) == 1:
        token = main
    else:
        raise ValueError(f"unsupported key '{main}'")

    return "".join(mods) + token


def escape_literal_keys(text: str) -> str:
    """Escape pywinauto send_keys metacharacters so literal text types as-is.

    Song titles like "海阔天空 (Live)" contain ()+^%~{} which send_keys would
    otherwise interpret as modifier grouping / special tokens.
    """
    return "".join(
        "{" + ch + "}" if ch in "+^%~(){}" else ch for ch in text
    )


def is_editable_control_type(ctype: str) -> bool:
    t = (ctype or "").lower()
    return any(k in t for k in _EDITABLE_TYPES)


def is_result_control_type(ctype: str) -> bool:
    t = (ctype or "").lower()
    return any(k in t for k in _RESULT_TYPES)


def is_query_field(ctype: str, name: str, name_filter: str) -> bool:
    """True when this node is the search box now displaying the query.

    After typing 夜曲, the Edit's name becomes 夜曲 — same string as the
    result rows. A name_contains inspect must not treat that Edit as a song.
    """
    needle = (name_filter or "").strip().lower()
    if not needle or not is_editable_control_type(ctype):
        return False
    return needle in (name or "").strip().lower()


def cache_entry_key(entry: dict[str, Any]) -> str:
    """Identity for inspect-cache merge. Name alone is not unique (search
    box and a result row can both be called 夜曲)."""
    auto = str(entry.get("auto_id") or "").strip().lower()
    if auto:
        return f"auto:{auto}"
    typ = str(entry.get("type") or "").strip().lower()
    name = str(entry.get("name") or "").strip().lower()
    rect = entry.get("rect") or (0, 0, 0, 0)
    y = int(rect[1]) if len(rect) > 1 else 0
    return f"{typ}:{name}:y{y}"


def is_chrome_leaf(ctype: str) -> bool:
    """Images are named chrome (logo / sidebar_*) with no useful descendants."""
    return "image" in (ctype or "").lower()


def keep_unfiltered_control(ctype: str, name: str, auto_id: str) -> bool:
    """Whether an unfiltered inspect should surface this node to the model.

    Chromium apps (NetEase etc.) expose thousands of unnamed Group/Pane nodes.
    Returning those first fills max_nodes and hides the named Edit/Text the
    agent actually needs. Filtered inspects (name_contains / control_type)
    skip this gate so the caller can still hunt by type.
    """
    t = (ctype or "").lower()
    if is_chrome_leaf(ctype):
        return False
    if (name or "").strip() or (auto_id or "").strip():
        return True
    return any(
        k in t
        for k in (
            "button",
            "edit",
            "document",
            "listitem",
            "menuitem",
            "hyperlink",
            "tabitem",
            "checkbox",
            "radiobutton",
            "combobox",
            "treeitem",
            "slider",
            "spinner",
            "splitbutton",
        )
    )


def get_foreground_window() -> WindowInfo | None:
    """Return the current foreground top-level window, if any."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    hwnd = int(user32.GetForegroundWindow())
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value or ""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(hwnd=hwnd, title=title, pid=int(pid.value))


# Last non-assistant window successfully targeted. Windows often refuses to
# steal focus from the assistant, so GetForegroundWindow stays on
# AgentAssistant::UI even after a successful focus_window — click/type then
# hit our own chat. Prefer this hwnd when the foreground is ourselves.
_last_target: WindowInfo | None = None


def remember_ui_target(win: WindowInfo | None) -> None:
    """Record the app window UIA should keep targeting (tests may clear)."""
    global _last_target
    if win is None or not win.hwnd:
        _last_target = None
        return
    if is_assistant_window(win):
        return
    _last_target = win


def _allow_self_target(pattern: str) -> bool:
    return pattern.strip().lower() == "agentassistant::ui"


def _drop_assistant(windows: list[WindowInfo], *, keep_self: bool) -> list[WindowInfo]:
    if keep_self:
        return windows
    return [w for w in windows if not is_assistant_window(w)]


def _windows_for_pattern(pattern: str) -> list[WindowInfo]:
    """Title match, then (if needed) app-registry exe match.

    NetEase Cloud Music's HWND title is the current song, not 「网易云音乐」.
    """
    keep_self = _allow_self_target(pattern)
    matches = _drop_assistant(find_windows(pattern), keep_self=keep_self)
    if matches:
        return matches
    try:
        from agent_assistant.app_registry import app_registry

        app = app_registry.find_app(pattern)
    except Exception:
        app = None
    if app is not None and app.exe_path:
        matches = _drop_assistant(
            find_windows_by_exe(app.exe_path), keep_self=False
        )
        if matches:
            return matches
    return []


def resolve_window(title_pattern: str | None = None) -> WindowInfo:
    """Resolve target window: by title/app, else last target, else foreground.

    Never returns AgentAssistant's own window unless title_pattern is exactly
    ``AgentAssistant::UI``. Raises LookupError when nothing matches.
    """
    pattern = (title_pattern or "").strip()
    if pattern:
        matches = _windows_for_pattern(pattern)
        if not matches:
            raise LookupError(
                f"no matching window for pattern '{pattern}'. "
                "App window titles may be the current document/song, not the "
                "product name — use launch_app (it returns window_title) or "
                "the process window title from a previous inspect."
            )
        target = matches[0]
        focus_window_hwnd(target.hwnd)
        remember_ui_target(target)
        return target

    fg = get_foreground_window()
    if fg is not None and fg.hwnd and not is_assistant_window(fg):
        remember_ui_target(fg)
        return fg

    last = _last_target
    if last is not None and last.hwnd:
        try:
            alive = bool(ctypes.windll.user32.IsWindow(last.hwnd))  # type: ignore[attr-defined]
        except Exception:
            alive = False
        if alive and not is_assistant_window(last):
            return last

    raise LookupError(
        "foreground is AgentAssistant itself; pass title_pattern for the "
        "target app (launch_app returns the real window_title)."
    )


def _safe_attr(ctrl: Any, *names: str, default: str = "") -> str:
    for name in names:
        try:
            val = getattr(ctrl, name, None)
            if callable(val):
                val = val()
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
        except Exception:
            continue
    return default


def _read_ident(ctrl: Any) -> tuple[str, str, str]:
    """(control_type, name, automation_id) — ONE element_info access.

    The hot path of both inspect and find; never read these twice per node.
    For the UIA backend element_info.name IS the window text, so no separate
    window_text() fallback round trip.
    """
    try:
        et = ctrl.element_info
        ctype = str(getattr(et, "control_type", None) or "").strip()
        name = (getattr(et, "name", None) or "").strip()
        auto_id = (getattr(et, "automation_id", None) or "").strip()
    except Exception:
        return (
            _safe_attr(ctrl, "friendly_class_name", default="Unknown"),
            _safe_attr(ctrl, "window_text"),
            "",
        )
    if not ctype:
        ctype = _safe_attr(ctrl, "friendly_class_name", default="Unknown")
    return ctype, name, auto_id


def _control_type(ctrl: Any) -> str:
    try:
        et = getattr(ctrl, "element_info", None)
        if et is not None and getattr(et, "control_type", None):
            return str(et.control_type)
    except Exception:
        pass
    return _safe_attr(ctrl, "friendly_class_name", default="Unknown")


def _is_visible(ctrl: Any) -> bool:
    """Loose visibility check: trust UIA if it works, else fall back to
    "has a non-empty bounding rectangle" (self-drawn list items often report
    is_visible=False even though the user can see them)."""
    try:
        if ctrl.is_visible():
            return True
    except Exception:
        pass
    # Fallback: non-zero rectangle on screen
    try:
        r = ctrl.rectangle()
        return bool(r and r.width() > 0 and r.height() > 0)
    except Exception:
        return True


def _is_enabled(ctrl: Any) -> bool:
    try:
        return bool(ctrl.is_enabled())
    except Exception:
        return True


def _is_actionable(
    ctrl_type: str,
    enabled: bool,
    *,
    name: str = "",
    rect: tuple[int, int, int, int] | None = None,
) -> bool:
    if not enabled:
        return False
    t = ctrl_type.lower()
    if any(
        k in t
        for k in (
            "button",
            "edit",
            "document",
            "listitem",
            "menuitem",
            "hyperlink",
            "tabitem",
            "checkbox",
            "radiobutton",
            "combobox",
            "treeitem",
            "slider",
            "spinner",
            "splitbutton",
        )
    ):
        return True
    # Chromium playlists (NetEase): the song row is a named Text with a rect
    return bool("text" in t and (name or "").strip() and rect)


class UiDriver:
    """Thin wrapper around pywinauto UIA for inspect / click / type / hotkey."""

    def __init__(self) -> None:
        # hwnd → (monotonic_ts, window wrapper)
        self._conn_cache: dict[int, tuple[float, Any]] = {}
        # hwnd → (monotonic_ts, [cache entries from the last inspect])
        self._inspect_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self.last_skipped_unnamed = 0

    def connect(self, hwnd: int) -> Any:
        now = time.monotonic()
        hit = self._conn_cache.get(hwnd)
        if hit is not None and now - hit[0] < _CONNECT_TTL_S:
            return hit[1]
        from pywinauto import Application

        app = Application(backend="uia").connect(handle=hwnd)
        win = app.window(handle=hwnd)
        self._conn_cache[hwnd] = (now, win)
        return win

    @staticmethod
    def _window_rect(hwnd: int) -> tuple[int, int]:
        """Top-left of the window client area in screen coords."""
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
                pt = wintypes.POINT(0, 0)
                ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
                return pt.x, pt.y
        except Exception:
            pass
        return 0, 0

    @staticmethod
    def _ctrl_rect(
        ctrl: Any, origin_x: int, origin_y: int
    ) -> tuple[int, int, int, int] | None:
        """Control rect relative to the window's client area."""
        try:
            r = ctrl.rectangle()
            w, h = int(r.width()), int(r.height())
            if w <= 0 or h <= 0:
                return None
            return (
                int(r.left) - origin_x,
                int(r.top) - origin_y,
                w,
                h,
            )
        except Exception:
            return None

    @staticmethod
    def _ctrl_rect_status(
        ctrl: Any, origin_x: int, origin_y: int
    ) -> tuple[tuple[int, int, int, int] | None, bool]:
        """(rect_or_None, visible) from a SINGLE rectangle read.

        Accepts pywinauto wrappers (``rectangle()``) and UIAElementInfo
        (``rectangle`` property). Zero-size → invisible. Read failure → keep
        the node (some wrappers can't report a rect).
        """
        try:
            getter = getattr(ctrl, "rectangle", None)
            if getter is None:
                info = getattr(ctrl, "element_info", None)
                getter = getattr(info, "rectangle", None) if info is not None else None
            if getter is None:
                return None, True
            r = getter() if callable(getter) else getter
            width_fn = getattr(r, "width", None)
            height_fn = getattr(r, "height", None)
            w = int(width_fn() if callable(width_fn) else (width_fn or 0))
            h = int(height_fn() if callable(height_fn) else (height_fn or 0))
            if w <= 0 or h <= 0:
                right = getattr(r, "right", None)
                bottom = getattr(r, "bottom", None)
                if right is not None and bottom is not None:
                    w = int(right) - int(r.left)
                    h = int(bottom) - int(r.top)
            if w <= 0 or h <= 0:
                return None, False
            return (int(r.left) - origin_x, int(r.top) - origin_y, w, h), True
        except Exception:
            return None, True

    @staticmethod
    def _read_ident_any(obj: Any) -> tuple[str, str, str]:
        """Identity from a wrapper or a raw UIAElementInfo."""
        if getattr(obj, "element_info", None) is not None:
            return _read_ident(obj)
        try:
            ctype = str(getattr(obj, "control_type", None) or "").strip()
            name = (getattr(obj, "name", None) or "").strip()
            auto_id = (getattr(obj, "automation_id", None) or "").strip()
        except Exception:
            return ("Unknown", "", "")
        return ctype or "Unknown", name, auto_id

    @staticmethod
    def _uia_has_findall(root: Any) -> bool:
        ei = getattr(root, "element_info", None)
        return callable(getattr(ei, "descendants", None))

    def _uia_descendants(self, root: Any, **kwargs: Any) -> list[Any]:
        """UIA FindAll. ``title=`` is an exact Name property match (fast path).

        ``limit`` (kwarg, popped) caps how many elements we materialize. Exact
        name matches stay small; type scans need a high cap so song rows are
        not dropped because sidebar Text filled the first 150 slots.
        """
        limit = kwargs.pop("limit", _FINDALL_SCAN_CAP)
        try:
            ei = getattr(root, "element_info", None)
            fn = getattr(ei, "descendants", None)
            if not callable(fn):
                return []
            found = fn(**kwargs)
        except Exception:
            logger.debug("uia descendants failed kwargs=%s", kwargs, exc_info=True)
            return []
        if found is None:
            return []
        if isinstance(found, (list, tuple)):
            seq = found
        else:
            if "Mock" in type(found).__name__:
                return []
            try:
                seq = list(found)
            except TypeError:
                return []
        if limit is None:
            return list(seq)
        return list(seq)[: int(limit)]

    def inspect(
        self,
        hwnd: int,
        *,
        max_depth: int = 4,
        max_nodes: int = 80,
        control_type: str | None = None,
        name_contains: str | None = None,
    ) -> list[ControlSummary]:
        """Walk visible descendants; return a truncated summary list.

        Optional filters (ANDed, case-insensitive substring):
        - ``control_type``: e.g. "ListItem", "Edit", "Document"
        - ``name_contains``: e.g. "夜曲"

        Unfiltered inspect is a shallow DFS (sidebar chrome first). Filtered
        ``name_contains`` uses UIA FindAll by Name so song rows behind thousands
        of unnamed Groups are still found. Search-box Edit showing the query
        is omitted so the model is not handed the input field as a "result".
        """
        max_depth = max(1, min(int(max_depth), 8))
        max_nodes = max(1, min(int(max_nodes), 200))
        ct_filter = (control_type or "").strip().lower()
        name_raw = (name_contains or "").strip()
        name_filter = name_raw.lower()
        origin_x, origin_y = self._window_rect(hwnd)

        root = self.connect(hwnd)
        out: list[ControlSummary] = []
        cache_entries: list[dict[str, Any]] = []
        visits = 0
        skipped_unnamed = 0
        filtered = bool(ct_filter or name_filter)
        seen: set[str] = set()
        via = "walk"
        visit_cap = _INSPECT_MAX_VISITS_NAMED if name_filter else _INSPECT_MAX_VISITS
        if name_filter:
            max_depth = 8

        def matches(ctype: str, name: str, auto_id: str) -> bool:
            if ct_filter and ct_filter not in ctype.lower():
                return False
            if name_filter and (
                name_filter not in name.lower()
                and name_filter not in auto_id.lower()
            ):
                return False
            return True

        def append_node(
            ctype: str,
            name: str,
            auto_id: str,
            path: str,
            rect: tuple[int, int, int, int] | None,
            parent_name: str,
            row_index: int | None,
            enabled: bool,
        ) -> bool:
            nonlocal skipped_unnamed
            if len(out) >= max_nodes:
                return False
            if name_filter and is_query_field(ctype, name, name_filter):
                skipped_unnamed += 1
                return False
            if not (filtered or keep_unfiltered_control(ctype, name, auto_id)):
                skipped_unnamed += 1
                return False
            entry = {
                "id": f"e{len(out)}",
                "name": name[:120],
                "auto_id": auto_id[:80],
                "type": ctype,
                "rect": rect,
            }
            key = cache_entry_key(entry)
            if key in seen:
                return False
            seen.add(key)
            uid = str(entry["id"])
            out.append(
                ControlSummary(
                    control_type=ctype,
                    name=name[:120],
                    automation_id=auto_id[:80],
                    path=path,
                    enabled=enabled,
                    actionable=_is_actionable(
                        ctype, enabled, name=name, rect=rect
                    ),
                    rect=rect,
                    uid=uid,
                    parent_name=(parent_name[:40] or None) if parent_name else None,
                    row_index=row_index,
                )
            )
            cache_entries.append(entry)
            return True

        def has_result_row() -> bool:
            return any(is_result_control_type(n.control_type) for n in out)

        def collect_name_hits() -> int:
            if not name_raw:
                return 0
            raw_hits: list[Any] = list(
                self._uia_descendants(root, title=name_raw, limit=_FINDALL_CAP)
            )
            if not any(
                is_result_control_type(self._read_ident_any(el)[0])
                for el in raw_hits
            ):
                for ct in _NAME_SEARCH_TYPES:
                    raw_hits.extend(self._uia_descendants(root, control_type=ct))
            added = 0
            for el in raw_hits:
                ctype, name, auto_id = self._read_ident_any(el)
                if not matches(ctype, name, auto_id):
                    continue
                rect, visible = self._ctrl_rect_status(el, origin_x, origin_y)
                if not visible or rect is None:
                    continue
                parent = ""
                try:
                    p = getattr(el, "parent", None)
                    if p is not None:
                        parent = (getattr(p, "name", None) or "").strip()
                except Exception:
                    parent = ""
                enabled = True
                try:
                    if hasattr(el, "is_enabled"):
                        enabled = _is_enabled(el)
                    elif hasattr(el, "enabled"):
                        enabled = bool(el.enabled)
                except Exception:
                    enabled = True
                if append_node(
                    ctype, name, auto_id, "findall", rect, parent, None, enabled
                ):
                    added += 1
            return added

        def walk(
            ctrl: Any,
            depth: int,
            path: str,
            parent_name: str,
            ctype: str,
            name: str,
            auto_id: str,
            row_index: int | None,
        ) -> None:
            nonlocal visits, skipped_unnamed
            if len(out) >= max_nodes or visits >= visit_cap:
                return
            visits += 1

            rect, visible = self._ctrl_rect_status(ctrl, origin_x, origin_y)
            # Unfiltered: skip invisible subtrees. Name hunt: Chromium wraps
            # result Text in zero-size Groups — still recurse.
            if depth > 0 and not visible and not name_filter:
                return

            if depth > 0 and visible and matches(ctype, name, auto_id):
                enabled = _is_enabled(ctrl)
                append_node(
                    ctype, name, auto_id, path, rect, parent_name, row_index, enabled
                )

            if depth >= max_depth or len(out) >= max_nodes:
                return
            if is_chrome_leaf(ctype):
                return

            try:
                children = ctrl.children()
            except Exception:
                return

            type_counts: dict[str, int] = {}
            for child in children:
                if len(out) >= max_nodes or visits >= visit_cap:
                    break
                c_ctype, c_name, c_auto = _read_ident(child)
                idx = type_counts.get(c_ctype, 0)
                type_counts[c_ctype] = idx + 1
                if c_name:
                    safe = c_name.replace("/", "_")[:40]
                    segment = f"{c_ctype}[{idx}:'{safe}']"
                else:
                    segment = f"{c_ctype}[{idx}]"
                child_path = f"{path}/{segment}" if path else segment
                walk(child, depth + 1, child_path, name, c_ctype, c_name, c_auto, idx)

        if name_filter:
            if collect_name_hits():
                via = "findall"
            if not has_result_row() and self._uia_has_findall(root):
                time.sleep(_NAME_RETRY_S)
                if collect_name_hits():
                    via = "findall-retry"
            if has_result_row():
                via = "findall" if via == "walk" else via
            else:
                r_ctype, r_name, r_auto = _read_ident(root)
                walk(root, 0, r_ctype, "", r_ctype, r_name, r_auto, None)
                if out:
                    via = "walk" if via == "walk" else f"{via}+walk"
        else:
            r_ctype, r_name, r_auto = _read_ident(root)
            walk(root, 0, r_ctype, "", r_ctype, r_name, r_auto, None)

        self.last_skipped_unnamed = skipped_unnamed
        if name_filter:
            out, cache_entries = self._prefer_result_nodes(out, cache_entries)
        if not filtered:
            self._inspect_cache[hwnd] = (time.monotonic(), cache_entries)
        else:
            self._merge_inspect_cache(hwnd, cache_entries)
        self._sync_node_uids(hwnd, out, cache_entries)
        preview = ", ".join(
            f"{n.uid}:{n.control_type}:{n.name[:16]}" for n in out[:12]
        )
        logger.info(
            "ui_inspect n=%s filter=%s via=%s visits=%s %s",
            len(out),
            name_filter or "-",
            via,
            visits,
            preview,
        )
        return out

    def _prefer_result_nodes(
        self,
        out: list[ControlSummary],
        cache_entries: list[dict[str, Any]],
    ) -> tuple[list[ControlSummary], list[dict[str, Any]]]:
        """Put song-like Text/ListItem ahead of leftover chrome in filtered inspect."""
        paired = list(zip(out, cache_entries))

        def rank(node: ControlSummary) -> tuple[int, int]:
            if is_result_control_type(node.control_type):
                y = node.rect[1] if node.rect else 0
                return (0, y)
            if is_editable_control_type(node.control_type):
                return (2, 0)
            return (1, node.rect[1] if node.rect else 0)

        paired.sort(key=lambda p: rank(p[0]))
        if not paired:
            return out, cache_entries
        nodes, entries = zip(*paired)
        return list(nodes), list(entries)

    def _sync_node_uids(
        self,
        hwnd: int,
        out: list[ControlSummary],
        cache_entries: list[dict[str, Any]],
    ) -> None:
        """Make payload ids match the merged inspect cache (not walk-local e0)."""
        cached = self._inspect_cache.get(hwnd)
        if cached is None:
            return
        by_key = {
            cache_entry_key(entry): str(entry.get("id") or "")
            for entry in cached[1]
            if entry.get("id")
        }
        for node, entry in zip(out, cache_entries):
            cid = by_key.get(cache_entry_key(entry))
            if cid:
                node.uid = cid
                entry["id"] = cid

    def _merge_inspect_cache(self, hwnd: int, entries: list[dict[str, Any]]) -> None:
        """Filtered inspects must not replace e-ids from the last full tree."""
        now = time.monotonic()
        cached = self._inspect_cache.get(hwnd)
        if cached is None or now - cached[0] > _INSPECT_CACHE_TTL_S:
            self._inspect_cache[hwnd] = (now, entries)
            return
        existing = list(cached[1])
        by_key = {cache_entry_key(e): e for e in existing}
        next_i = len(existing)
        for e in entries:
            key = cache_entry_key(e)
            hit = by_key.get(key)
            if hit is not None:
                hit["rect"] = e.get("rect")
                hit["type"] = e.get("type") or hit.get("type")
                continue
            merged = dict(e)
            merged["id"] = f"e{next_i}"
            next_i += 1
            existing.append(merged)
            by_key[key] = merged
        self._inspect_cache[hwnd] = (now, existing)

    # ── Target resolution ──────────────────────────────────────────────────

    def _cache_lookup(
        self,
        hwnd: int,
        target: str,
        *,
        prefer_editable: bool | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a click/type target from the last inspect of this window.

        Zero COM calls. Returns the entry (with window-relative rect) or None
        when the cache is cold/stale or nothing matches.

        When several nodes share a name (search Edit and a 夜曲 row),
        ``prefer_editable=True`` picks the Edit; ``False`` picks a Text/ListItem.
        """
        needle = (target or "").strip()
        if not needle:
            return None
        cached = self._inspect_cache.get(hwnd)
        if cached is None or time.monotonic() - cached[0] > _INSPECT_CACHE_TTL_S:
            return None
        needle_l = needle.lower()
        best_score = 0
        hits: list[dict[str, Any]] = []
        for entry in cached[1]:
            if not entry.get("rect"):
                continue
            name_l = str(entry.get("name") or "").lower()
            auto_l = str(entry.get("auto_id") or "").lower()
            uid_l = str(entry.get("id") or "").lower()
            score = 0
            if uid_l and uid_l == needle_l:
                score = 100
            elif auto_l and auto_l == needle_l:
                score = 95
            elif name_l and name_l == needle_l:
                score = 90
            elif auto_l and needle_l in auto_l:
                score = 70
            elif name_l and needle_l in name_l:
                score = 60
            if not score:
                continue
            if score > best_score:
                best_score = score
                hits = [entry]
                if score >= 100:
                    break
            elif score == best_score:
                hits.append(entry)
        if not hits:
            return None
        if len(hits) == 1 or prefer_editable is None:
            return hits[0]
        if prefer_editable:
            for entry in hits:
                if is_editable_control_type(str(entry.get("type") or "")):
                    return entry
            return hits[0]
        for entry in hits:
            if is_result_control_type(str(entry.get("type") or "")):
                return entry
        for entry in hits:
            if not is_editable_control_type(str(entry.get("type") or "")):
                return entry
        return hits[0]

    def _iter_descendants(self, hwnd: int) -> Iterator[Any]:
        """Bounded BFS over the control tree (generator).

        Replaces pywinauto descendants() — a full-tree FindAll that takes
        seconds on Chromium apps with thousands of nodes.
        """
        try:
            root = self.connect(hwnd)
        except Exception:
            return
        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        yielded = 0
        while queue:
            ctrl, depth = queue.popleft()
            if depth > 0:
                yield ctrl
                yielded += 1
                if yielded >= _FIND_MAX_NODES:
                    return
            if depth >= _FIND_MAX_DEPTH:
                continue
            try:
                children = ctrl.children()
            except Exception:
                continue
            for child in children:
                queue.append((child, depth + 1))

    def find_control(
        self, hwnd: int, target: str
    ) -> tuple[Any | None, list[str]]:
        """Find a control by automation_id, name, or path substring (live walk).

        Returns (control_or_None, up_to_5_candidate_names). Exact id/name
        matches short-circuit the walk.
        """
        needle = (target or "").strip()
        if not needle:
            return None, []

        needle_l = needle.lower()
        best: tuple[int, Any, str] | None = None
        peers: list[str] = []
        nearby: list[str] = []

        for ctrl in self._iter_descendants(hwnd):
            ctype, name, auto_id = _read_ident(ctrl)
            label = name or auto_id or ctype
            score = 0
            if auto_id and auto_id.lower() == needle_l:
                score = 100
            elif name and name.lower() == needle_l:
                score = 90
            elif auto_id and needle_l in auto_id.lower():
                score = 70
            elif name and needle_l in name.lower():
                score = 60
            else:
                composite = f"{ctype}/{name}".lower()
                if needle_l in composite:
                    score = 40
            if score:
                if best is None or score > best[0]:
                    best = (score, ctrl, label)
                    peers = [label]
                elif score == best[0] and len(peers) < 5:
                    peers.append(label)
                if score >= 90:
                    return ctrl, peers
            elif name and len(nearby) < 5 and name not in nearby:
                nearby.append(name)

        if best is None:
            return None, nearby
        return best[1], peers

    # ── Post-action feedback ───────────────────────────────────────────────

    def _after_state(self, settle_s: float = 0.12) -> dict[str, Any] | None:
        """Cheap post-action snapshot: foreground window title via Win32.

        Do not call UIA GetFocusedElement here. Chromium apps (NetEase Cloud)
        hand back focused COM elements that later crash the whole MCP process
        on GC — Windows STATUS_ACCESS_VIOLATION (exit 3221225477) — typically
        during the next model round-trip, after ui_click already returned ok.
        """
        try:
            if settle_s > 0:
                time.sleep(settle_s)
            fg = get_foreground_window()
            if fg is not None and fg.title:
                return {"foreground": fg.title[:80]}
            return None
        except Exception:
            return None

    # ── Actions ────────────────────────────────────────────────────────────

    def click(
        self,
        hwnd: int,
        target: str | None = None,
        button: str = "left",
        *,
        x: int | None = None,
        y: int | None = None,
        dx: int = 0,
        dy: int = 0,
    ) -> dict[str, Any]:
        """Click a control by name/id, or a coordinate inside the window client area.

        Priority:
        1. x/y given → click that point (after optional dx/dy nudge)
        2. target resolved from the last inspect cache → click its rect center
        3. target via live bounded search → click the control
        """
        btn = (button or "left").lower()
        if btn not in ("left", "right", "double"):
            raise ValueError("button must be left, right, or double")

        if x is not None and y is not None:
            data = self._click_point(hwnd, int(x) + int(dx), int(y) + int(dy), btn)
            data["after"] = self._after_state()
            logger.info(
                "ui_click target=%s button=%s clicked=%s via=xy",
                target,
                btn,
                data.get("clicked"),
            )
            return data

        if not target:
            raise ValueError("either target or x/y must be provided")

        entry = self._cache_lookup(hwnd, target, prefer_editable=False)
        if entry is not None:
            rect = entry["rect"]
            cx = rect[0] + rect[2] // 2 + int(dx)
            cy = rect[1] + rect[3] // 2 + int(dy)
            data = self._click_point(hwnd, cx, cy, btn)
            data["clicked"] = entry.get("name") or entry.get("id") or target
            data["via"] = "inspect_cache"
            data["control"] = {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "type": entry.get("type"),
            }
            data["after"] = self._after_state()
            logger.info(
                "ui_click target=%s button=%s clicked=%s type=%s via=cache",
                target,
                btn,
                data.get("clicked"),
                entry.get("type"),
            )
            return data

        ctrl, peers = self.find_control(hwnd, target)
        if ctrl is None:
            hint = ", ".join(peers) if peers else "(none)"
            raise LookupError(
                f"no control matching '{target}'. Nearby names: {hint}"
            )

        if dx or dy:
            rect = self._ctrl_rect(ctrl, *self._window_rect(hwnd))
            if not rect:
                raise LookupError(f"control '{target}' has no rectangle")
            cx, cy = rect[0] + rect[2] // 2 + int(dx), rect[1] + rect[3] // 2 + int(dy)
            data = self._click_point(hwnd, cx, cy, btn)
            data["after"] = self._after_state()
            return data

        focus_window_hwnd(hwnd)
        if btn == "double":
            ctrl.double_click_input()
        elif btn == "right":
            ctrl.click_input(button="right")
        else:
            ctrl.click_input(button="left")

        try:
            et = ctrl.element_info
            clicked = (getattr(et, "name", None) or target).strip()
        except Exception:
            clicked = target
        return {
            "clicked": clicked,
            "button": btn,
            "peers": peers[:3],
            "after": self._after_state(),
        }

    def _click_point(self, hwnd: int, x: int, y: int, btn: str) -> dict[str, Any]:
        """Click a point relative to the window client area."""
        import pywinauto.mouse as mouse

        origin_x, origin_y = self._window_rect(hwnd)
        screen_x, screen_y = origin_x + x, origin_y + y
        focus_window_hwnd(hwnd)
        if btn == "double":
            mouse.double_click(button="left", coords=(screen_x, screen_y))
        elif btn == "right":
            mouse.click(button="right", coords=(screen_x, screen_y))
        else:
            mouse.click(button="left", coords=(screen_x, screen_y))
        return {"clicked": f"({x},{y})", "button": btn, "coords": {"x": x, "y": y}}

    def scroll(
        self,
        hwnd: int,
        direction: str = "down",
        amount: int = 3,
        target: str | None = None,
    ) -> dict[str, Any]:
        """Scroll a window or specific control.

        direction: up/down/left/right
        amount: number of wheel clicks (positive) or lines to scroll
        target: optional control name to scroll within (default: window center)
        """
        import pywinauto.mouse as mouse

        direction = direction.lower()
        if direction not in ("up", "down", "left", "right"):
            raise ValueError("direction must be up/down/left/right")

        amount = max(1, min(int(amount), 20))  # cap at 20 for safety

        focus_window_hwnd(hwnd)

        rect: tuple[int, int, int, int] | None = None
        if target:
            entry = self._cache_lookup(hwnd, target)
            if entry is not None:
                rect = tuple(entry["rect"])  # type: ignore[assignment]
            else:
                ctrl, peers = self.find_control(hwnd, target)
                if ctrl is None:
                    hint = ", ".join(peers) if peers else "(none)"
                    raise LookupError(f"no control matching '{target}'. Nearby: {hint}")
                rect = self._ctrl_rect(ctrl, *self._window_rect(hwnd))
                if not rect:
                    raise LookupError(f"control '{target}' has no rectangle")
        if rect:
            cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        else:
            # Scroll at window center
            try:
                import ctypes
                from ctypes import wintypes
                crect = wintypes.RECT()
                ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(crect))
                cx, cy = (crect.right - crect.left) // 2, (crect.bottom - crect.top) // 2
            except Exception:
                cx, cy = 400, 300  # fallback

        origin_x, origin_y = self._window_rect(hwnd)
        screen_x, screen_y = origin_x + cx, origin_y + cy

        # Map direction to wheel delta (negative = down/forward, positive = up/back)
        wheel_map = {"down": -1, "up": 1, "right": -1, "left": 1}
        wheel_dist = wheel_map[direction] * amount

        # For horizontal scroll, hold Shift while scrolling
        if direction in ("left", "right"):
            import pywinauto.keyboard as keyboard
            keyboard.send_keys("{SHIFT down}")
            try:
                mouse.scroll(coords=(screen_x, screen_y), wheel_dist=wheel_dist)
            finally:
                keyboard.send_keys("{SHIFT up}")
        else:
            mouse.scroll(coords=(screen_x, screen_y), wheel_dist=wheel_dist)

        return {"scrolled": direction, "amount": amount, "target": target or "window_center", "coords": {"x": cx, "y": cy}, "after": self._after_state()}

    def _prefer_editable_entry(
        self, hwnd: int, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """If the cached target is a label (Text「搜索」), prefer a nearby Edit.

        NetEase Cloud's inspect often surfaces the placeholder Text, not the
        Edit. Clicking that label never gives the search box keyboard focus, so
        ui_type reports ok while the query stays on the previous song.
        """
        if is_editable_control_type(str(entry.get("type") or "")):
            return entry
        cached = self._inspect_cache.get(hwnd)
        if cached is None:
            return entry
        name = str(entry.get("name") or "").strip().lower()
        edits = [
            e
            for e in cached[1]
            if e.get("rect") and is_editable_control_type(str(e.get("type") or ""))
        ]
        if not edits:
            return entry
        for candidate in edits:
            if str(candidate.get("name") or "").strip().lower() == name:
                return candidate
        if name in ("搜索", "search", "查找"):
            return min(edits, key=lambda e: (e["rect"][1], e["rect"][0]))
        return entry

    def _try_child_window(self, root: Any, spec: dict[str, str]) -> Any | None:
        try:
            spec_obj = root.child_window(**spec)
            if spec_obj.exists(timeout=0.35):
                return spec_obj.wrapper_object()
        except Exception:
            return None
        return None

    def _resolve_control(self, hwnd: int, entry: dict[str, Any]) -> Any | None:
        """FindFirst the cached control (auto_id / Edit+title). Not a full walk."""
        try:
            root = self.connect(hwnd)
        except Exception:
            return None
        auto_id = str(entry.get("auto_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        ctype = str(entry.get("type") or "").strip()
        specs: list[dict[str, str]] = []
        if auto_id:
            specs.append({"auto_id": auto_id})
        if name and ctype and ctype.lower() not in ("text", "group", "pane", "image"):
            specs.append({"title": name, "control_type": ctype})
        if name:
            specs.append({"title": name, "control_type": "Edit"})
        for spec in specs:
            ctrl = self._try_child_window(root, spec)
            if ctrl is not None:
                return ctrl
        return None

    @staticmethod
    def _read_edit_value(ctrl: Any) -> str | None:
        for getter in (
            lambda: ctrl.get_value(),
            lambda: (ctrl.legacy_properties() or {}).get("Value"),
            lambda: ctrl.window_text(),
        ):
            try:
                value = getter()
            except Exception:
                continue
            if isinstance(value, str):
                return value
        return None

    def _focus_edit(
        self, hwnd: int, entry: dict[str, Any] | None, ctrl: Any | None
    ) -> None:
        if ctrl is not None:
            try:
                ctrl.click_input()
                return
            except Exception:
                try:
                    ctrl.set_focus()
                    return
                except Exception:
                    pass
        rect = (entry or {}).get("rect") if entry else None
        if rect:
            self._click_point(
                hwnd, rect[0] + rect[2] // 2, rect[1] + rect[3] // 2, "left"
            )

    def _finish_type(
        self,
        text: str,
        target: str | None,
        clear: bool,
        via: str,
        field_value: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        after = self._after_state()
        landed: bool | None
        if field_value is None:
            landed = None
        else:
            landed = text in field_value
        data: dict[str, Any] = {
            "typed": text,
            "target": target,
            "cleared": clear,
            "via": via,
            "field_value": field_value,
            "landed": landed,
            "after": after,
        }
        if extra:
            data.update(extra)
        logger.info(
            "ui_type via=%s target=%s landed=%s field=%r fg=%s",
            via,
            target,
            landed,
            (field_value or "")[:40],
            (after or {}).get("foreground") if isinstance(after, dict) else None,
        )
        return data

    def _input_literal(self, text: str, *, replace: bool) -> str:
        """Type ``text`` into the focused control with send_keys (no clipboard)."""
        from pywinauto.keyboard import send_keys

        if replace:
            send_keys("^a{BACKSPACE}", pause=0.02)
        send_keys(escape_literal_keys(text), with_spaces=True, pause=0.02)
        return "keys"

    def type_text(
        self,
        hwnd: int,
        text: str,
        *,
        clear: bool = False,
        target: str | None = None,
    ) -> dict[str, Any]:
        focus_window_hwnd(hwnd)
        ctrl = None
        entry: dict[str, Any] | None = None
        extra: dict[str, Any] = {}

        if target and target.strip():
            entry = self._cache_lookup(hwnd, target, prefer_editable=True)
            if entry is not None:
                entry = self._prefer_editable_entry(hwnd, entry)
                extra["control"] = {
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "type": entry.get("type"),
                }
                ctrl = self._resolve_control(hwnd, entry)
            else:
                ctrl, peers = self.find_control(hwnd, target)
                if ctrl is None:
                    hint = ", ".join(peers) if peers else "(none)"
                    raise LookupError(
                        f"no control matching '{target}'. Nearby names: {hint}"
                    )
            self._focus_edit(hwnd, entry, ctrl)
            time.sleep(_TYPE_FOCUS_S)
        else:
            via = self._input_literal(text, replace=clear)
            return self._finish_type(text, None, clear, via, None)

        if clear and ctrl is not None:
            try:
                ctrl.type_keys("^a{BACKSPACE}", pause=0.02)
            except Exception:
                pass

        via = "keys"
        if ctrl is not None:
            try:
                ctrl.set_edit_text(text)
                via = "value"
            except Exception:
                via = self._input_literal(text, replace=clear)
        else:
            via = self._input_literal(text, replace=clear)

        field_value = self._read_edit_value(ctrl) if ctrl is not None else None
        if field_value is not None and text not in field_value:
            via = self._input_literal(text, replace=True)
            field_value = self._read_edit_value(ctrl)
            extra["retried_keys"] = True

        return self._finish_type(text, target, clear, via, field_value, extra)

    def hotkey(self, hwnd: int, keys: str) -> dict[str, Any]:
        token = parse_hotkey(keys)
        focus_window_hwnd(hwnd)
        from pywinauto.keyboard import send_keys

        send_keys(token, pause=0.02)
        return {"keys": keys, "sent": token, "after": self._after_state()}


# Process-wide default driver (tests may monkeypatch)
ui_driver = UiDriver()
