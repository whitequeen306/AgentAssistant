"""Windows-specific utilities: window management, process helpers."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Win32 constants
SW_RESTORE = 9
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

# Our own UI / console titles — UIA must not click/type these by accident.
_ASSISTANT_TITLES = frozenset({"agentassistant::ui", "agentassistant"})


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    pid: int


def find_windows(title_pattern: str) -> list[WindowInfo]:
    """Find all visible windows whose title contains the pattern (case-insensitive)."""
    results: list[WindowInfo] = []
    pattern_lower = title_pattern.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if pattern_lower in title.lower():
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            results.append(WindowInfo(hwnd=hwnd, title=title, pid=pid.value))
        return True

    user32.EnumWindows(enum_callback, 0)
    return results


def is_assistant_window(win: WindowInfo) -> bool:
    """True for AgentAssistant's own UI (or its console) — not a user app."""
    title = (win.title or "").strip().lower()
    if title in _ASSISTANT_TITLES or title.startswith("agentassistant::"):
        return True
    try:
        if int(win.pid) == os.getpid():
            return True
    except (TypeError, ValueError):
        pass
    return False


def _process_image_path(pid: int) -> str:
    """Full exe path for a PID, or empty when inaccessible."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.wintypes.DWORD(32768)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_windows_by_exe(exe_path_or_stem: str) -> list[WindowInfo]:
    """Visible top-level windows whose process image matches exe path or stem.

    Music/video apps (NetEase Cloud Music, etc.) set the HWND title to the
    current track, so title-pattern matching against the product name fails.
    """
    needle = (exe_path_or_stem or "").strip()
    if not needle:
        return []
    path = Path(needle)
    stem = path.stem.lower()
    full = str(path).replace("/", "\\").lower() if path.suffix else ""
    results: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def enum_callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = _process_image_path(int(pid.value))
        if not image:
            return True
        image_l = image.replace("/", "\\").lower()
        image_stem = Path(image).stem.lower()
        if image_stem == stem or (full and image_l == full):
            results.append(WindowInfo(hwnd=hwnd, title=title, pid=int(pid.value)))
        return True

    user32.EnumWindows(enum_callback, 0)
    return results


def wait_for_process_window(
    exe_path_or_stem: str,
    *,
    timeout_s: float = 8.0,
    interval_s: float = 0.4,
) -> WindowInfo | None:
    """Poll until a non-assistant window for this exe appears."""
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    while time.monotonic() < deadline:
        for win in find_windows_by_exe(exe_path_or_stem):
            if not is_assistant_window(win):
                return win
        time.sleep(max(0.1, float(interval_s)))
    return None


def focus_window_hwnd(hwnd: int) -> bool:
    """Bring a window to the foreground and restore it if minimized.

    Uses ``IsIconic`` instead of ``WINDOWPLACEMENT`` — the latter is not
    exposed on all Python ``ctypes.wintypes`` builds and previously crashed
    ``focus_window`` / UI automation with AttributeError.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    # Set foreground
    user32.SetForegroundWindow(hwnd)
    # Ensure topmost briefly to steal focus
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    return True


def launch_process(path: str, workdir: str | None = None) -> int:
    """Launch a process, return PID. Uses start command for shell execution."""
    try:
        proc = subprocess.Popen(
            ["cmd", "/c", "start", "", path],
            cwd=workdir,
            creationflags=subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid
    except OSError:
        # Fallback: direct exec
        proc = subprocess.Popen(
            path,
            cwd=workdir,
            shell=True,
            creationflags=subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid


def launch_protocol_uri(uri: str) -> None:
    """Launch a protocol URI (e.g. 'whatsapp:' for UWP apps)."""
    subprocess.Popen(
        ["cmd", "/c", "start", "", uri],
        creationflags=subprocess.DETACHED_PROCESS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
