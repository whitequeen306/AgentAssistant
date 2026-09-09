"""get_selection tool — Get text selected in the currently focused app.

Strategy (per docs/06-tool-spec.md §1.9):
1. Try UI Automation (clean, no clipboard touch)
2. Fall back to Ctrl+C + clipboard backup/restore (universal)

The clipboard side-effect is encapsulated internally — invisible to the model.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from agent_assistant.tools.base import Tool, ToolParameter, ToolResult


def _get_selection_via_uia() -> str | None:
    """Try to get selected text via UI Automation (comtypes/uiautomation)."""
    try:
        import uiautomation as auto  # type: ignore[import-untyped]

        control = auto.GetFocusedControl()
        if control is None:
            return None

        # Try TextPattern (works for most text editors, browsers)
        try:
            pattern = control.GetTextPattern()
            if pattern:
                selection = pattern.GetSelection()
                if selection:
                    text = selection[0].GetText(-1)
                    if text and text.strip():
                        return text
        except Exception:
            pass

        # Try ValuePattern (some controls expose value)
        try:
            pattern = control.GetValuePattern()
            if pattern and pattern.Value:
                return pattern.Value
        except Exception:
            pass

        return None
    except ImportError:
        return None
    except Exception:
        return None


def _get_selection_via_clipboard() -> str | None:
    """Fallback: simulate Ctrl+C, read clipboard, restore original clipboard.

    Side-effect encapsulation: backs up and restores the user's clipboard.
    """
    try:
        import ctypes

        # Step 1: Backup current clipboard content
        backup_text = _read_clipboard()

        # Step 2: Clear clipboard and simulate Ctrl+C
        _clear_clipboard()
        time.sleep(0.05)

        # Simulate Ctrl+C via keybd_event
        VK_CONTROL = 0x11
        VK_C = 0x43
        KEYEVENTF_KEYUP = 0x0002

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_C, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

        time.sleep(0.15)  # Wait for clipboard to be populated

        # Step 3: Read the selected text
        selected_text = _read_clipboard()

        # Step 4: Restore original clipboard
        if backup_text is not None:
            _write_clipboard(backup_text)
        else:
            _clear_clipboard()

        if selected_text and selected_text.strip():
            return selected_text
        return None

    except Exception:
        return None


def _read_clipboard() -> str | None:
    """Read text from Windows clipboard."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        CF_UNICODETEXT = 13

        if not user32.OpenClipboard(None):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            kernel32.GlobalLock.restype = ctypes.c_wchar_p
            text = kernel32.GlobalLock(handle)
            kernel32.GlobalUnlock(handle)
            return text
        finally:
            user32.CloseClipboard()
    except Exception:
        return None


def _write_clipboard(text: str) -> None:
    """Write text to Windows clipboard."""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            p_mem = kernel32.GlobalLock(h_mem)
            ctypes.memmove(p_mem, data, len(data))
            kernel32.GlobalUnlock(h_mem)
            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        finally:
            user32.CloseClipboard()
    except Exception:
        pass


def _clear_clipboard() -> None:
    """Clear the Windows clipboard."""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()
    except Exception:
        pass


class GetSelectionTool(Tool):
    @property
    def name(self) -> str:
        return "get_selection"

    @property
    def description(self) -> str:
        return (
            "Get the text the user has selected in the currently focused app. "
            "When to call: entry point for right-click toolbox (before translate/summarize/ask), "
            "grabbing content for cross-app actions; often the next step for 'summarize this'. "
            "Prefers UI Automation (clean); falls back to Ctrl+C reading the clipboard if that fails. "
            "When NOT to call: reading files → use read_file; reading web pages → use read_page."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return []  # No parameters needed

    def execute(self, **kwargs: Any) -> ToolResult:
        # Strategy 1: UI Automation (clean, no side effects)
        text = _get_selection_via_uia()
        if text:
            return ToolResult.success(data={"text": text, "method": "ui_automation"})

        # Strategy 2: Ctrl+C + clipboard backup/restore
        text = _get_selection_via_clipboard()
        if text:
            return ToolResult.success(data={"text": text, "method": "clipboard"})

        # Both failed
        return ToolResult.failure(
            "no selection detected. The focused app may not support text selection, "
            "or nothing is selected. User can Ctrl+C manually.",
            code=404,
        )
