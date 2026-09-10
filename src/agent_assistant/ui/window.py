"""pywebview window management for the Dynamic-Island UI.

Three morphs only:
- docked-search: top search pill
- popup-chat:    compact chat after sending from the pill
- main:          full app (sidebar + content)

J6: Win32 WM_NCLBUTTONDOWN drag for frameless window.
Transparency: color-key (#000001) via LWA_COLORKEY — see _apply_win32_colorkey.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import webview

from agent_assistant.ui.bridge import api_bridge

logger = logging.getLogger(__name__)

# Win32 constants
_WM_NCLBUTTONDOWN = 0x00A1
_HT_CAPTION = 2
_GWL_EXSTYLE = -20
_GWL_STYLE = -16
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_WS_THICKFRAME = 0x00040000
_WS_MAXIMIZEBOX = 0x00010000
_LWA_COLORKEY = 0x00000001
_SWP_FRAMECHANGED = 0x0020
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE = 9
# Color key: CSS body paints #000001, DWM keys it out to show the desktop.
# COLORREF layout is 0x00BBGGRR → blue=1 → 0x00010000.
_COLORKEY = 0x00010000
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

# Edge-snap threshold (§5.6: ~20px)
SNAP_THRESHOLD = 20
# Restore threshold — larger than snap-in to create hysteresis so the window
# doesn't oscillate between docked and main near the edge.
RESTORE_THRESHOLD = 60
DRAG_MAX = 200          # px of downward drag to fully grow search bar → main
DRAG_SETTLE = 100       # px threshold: drag past this → settle to main, else snap back

# Window sizes per state. docked-search stays a narrow pill. Main is
# SCREEN-RELATIVE (≈68% × 82% of the work area, clamped) so it always reads
# like real software — a fixed 980×640 read like a dialog box and felt
# especially jarring when grown from the search pill.
STATE_SIZES = {
    "main": (1200, 800),     # fallback when no screen info is available
    "popup-chat": (480, 560),
}
VALID_STATES = frozenset({"main", "docked-search", "popup-chat"})
# User-draggable main window bounds. Min leaves ~640px for chat after sidebar.
MAIN_MIN_SIZE = (880, 520)
# Screen-relative main clamps: the min keeps the window app-like even on
# small laptops; the max stops ultra-wide monitors from stretching the
# chat column into unreadable line lengths.
MAIN_RELATIVE_MIN = (1024, 700)
MAIN_RELATIVE_MAX = (1560, 960)
DOCKED_SEARCH_HEIGHT = 48  # just the bar — no in-window gap (transparency is
# unreliable on some setups, so an in-window rope gap would render white).
DOCKED_SEARCH_BANNER_HEIGHT = 44  # confirm tip strip under the search pill
DOCKED_SEARCH_TOP_GAP = 24  # float this far below the work-area top
DOCKED_SEARCH_WIDTH_RATIO = 0.4  # legacy ratio — only caps the search pill
DOCKED_SEARCH_BAR_WIDTH = 420  # the suspended bar is a narrow pill, not a slab


def _clamp_main_size(w: int, h: int, sw: int, sh: int) -> tuple[int, int]:
    """Keep main usable on the current screen (never a postage-stamp chat)."""
    max_w = max(320, sw - 24)
    max_h = max(240, sh - 48)
    min_w = min(MAIN_MIN_SIZE[0], max_w)
    min_h = min(MAIN_MIN_SIZE[1], max_h)
    return (
        max(min_w, min(int(w), max_w)),
        max(min_h, min(int(h), max_h)),
    )


def state_size(state: str, screen: tuple[int, int]) -> tuple[int, int]:
    """Pure: compute (w, h) for a state given screen size.

    Main is screen-relative (≈68% × 82% of the work area, clamped to
    MAIN_RELATIVE_MIN/MAX) so initial open AND docked→main growth both read
    like a full app window on any display.
    """
    sw, sh = screen
    # Legacy morphs → search pill
    if state in ("minimized", "docked-sliver"):
        state = "docked-search"
    if state == "main":
        if sw > 0 and sh > 0:
            dw = max(MAIN_RELATIVE_MIN[0], min(MAIN_RELATIVE_MAX[0], int(sw * 0.68)))
            dh = max(MAIN_RELATIVE_MIN[1], min(MAIN_RELATIVE_MAX[1], int(sh * 0.82)))
        else:
            dw, dh = STATE_SIZES["main"]
        return _clamp_main_size(dw, dh, sw, sh)
    if state == "docked-search":
        return (min(DOCKED_SEARCH_BAR_WIDTH, int(sw * DOCKED_SEARCH_WIDTH_RATIO)), DOCKED_SEARCH_HEIGHT)
    if state == "popup-chat":
        pw, ph = STATE_SIZES["popup-chat"]
        return (min(pw, max(320, sw - 24)), min(ph, max(240, sh - 48)))
    raise ValueError(f"unknown state: {state}")


def detect_edge_snap(
    rect: tuple[int, int, int, int],
    screen: tuple[int, int],
    current_state: str,
    snap_enabled: bool,
    threshold: int = SNAP_THRESHOLD,
    restore_threshold: int = RESTORE_THRESHOLD,
) -> tuple[str, str | None] | None:
    """Pure: decide if a snap should happen after a drag.

    rect = (left, top, right, bottom). Returns (new_state, edge) where edge is
    the matched edge ("top"/"bottom"/"left"/"right") or None, or None if no snap.
    Priority: top > bottom > left > right (corner cases pick top).

    Two thresholds: ``threshold`` for main→docked snap-in (~20px);
    ``restore_threshold`` for docked→main restore (~60px) — the larger
    restore gap creates hysteresis so the window doesn't oscillate.
    """
    left, top, right, bottom = rect
    sw, sh = screen
    near_top = top <= threshold
    near_bottom = bottom >= sh - threshold
    near_left = left <= threshold
    near_right = right >= sw - threshold

    if current_state == "main":
        # Main never auto-snaps — it stays main however the user drags it.
        # (Top snap and left/right sliver snap both removed by request, so the
        # 'vertical blue line' can no longer appear from a main-page drag.)
        return None

    if current_state == "docked-search":
        rt = restore_threshold
        near_top_r = top <= rt
        near_bottom_r = bottom >= sh - rt
        near_left_r = left <= rt
        near_right_r = right >= sw - rt
        if not (near_top_r or near_bottom_r or near_left_r or near_right_r):
            return ("main", None)
        return None

    # popup-chat: no edge snap
    return None


def drag_progress(drag_delta: int, max_drag: int = 200) -> float:
    """Pure: map a drag distance to a 0..1 progress (clamped to [0, 1])."""
    if max_drag <= 0:
        return 1.0 if drag_delta > 0 else 0.0
    return max(0.0, min(1.0, drag_delta / max_drag))


def drag_target_height(progress: float, start_h: int, target_h: int) -> int:
    """Pure: interpolate height from start to target by progress (0..1)."""
    return int(start_h + (target_h - start_h) * progress)


def decide_drag_settle(state: str, drag_delta_y: int, threshold: int = 100) -> str:
    """Pure: decide target state after a drag release.

    docked-search dragged down past threshold -> 'main' (grow into full page);
    below threshold -> 'docked-search' (snap back). Other states unchanged
    (the caller handles edge-snap for main).
    """
    if state == "docked-search":
        return "main" if drag_delta_y >= threshold else "docked-search"
    return state


_screen_cache = None
_screen_cached_at = 0.0


def _screen_size() -> tuple[int, int]:
    """Screen size with a 5s TTL cache (avoids 2 Win32 syscalls per drag frame)."""
    global _screen_cache, _screen_cached_at
    import time
    now = time.monotonic()
    if _screen_cache is not None and (now - _screen_cached_at) < 5.0:
        return _screen_cache
    if sys.platform == "win32":
        user32 = ctypes.windll.user32
        _screen_cache = (
            user32.GetSystemMetrics(_SM_CXSCREEN),
            user32.GetSystemMetrics(_SM_CYSCREEN),
        )
    else:
        _screen_cache = (1920, 1080)
    _screen_cached_at = now
    return _screen_cache


def _work_area_for_hwnd(hwnd: int | None) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the monitor work area for hwnd.

    Falls back to the primary screen when Win32 monitor APIs are unavailable.
    """
    sw, sh = _screen_size()
    if sys.platform != "win32" or not hwnd:
        return 0, 0, sw, sh
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", _RECT),
                ("rcWork", _RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return 0, 0, sw, sh
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return 0, 0, sw, sh
        left, top = int(mi.rcWork.left), int(mi.rcWork.top)
        width = max(1, int(mi.rcWork.right - mi.rcWork.left))
        height = max(1, int(mi.rcWork.bottom - mi.rcWork.top))
        return left, top, width, height
    except Exception:
        return 0, 0, sw, sh


def _find_self_hwnd() -> int | None:
    """Find this process's visible top-level window (the webview) via
    EnumWindows + process-ID match.

    Robust against the 启动.bat console: that console belongs to cmd.exe (a
    different process), so filtering by the python process's own PID never
    returns it. This replaces the old FindWindow-by-title + GetForegroundWindow
    approach, which cached the console HWND (foreground at `shown` time) and
    made live_resize adopt the console's geometry — the 'drag → window
    expands to fill the desktop' bug.
    """
    if sys.platform != "win32":
        return None
    pid = os.getpid()
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd: int, _l: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid:
            found.append(int(hwnd))
        return True

    ctypes.windll.user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return None
    # Pick the largest visible top-level window in this process (the webview,
    # not any hidden helper window).
    best = None
    best_area = -1
    for h in found:
        r = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(h, ctypes.byref(r)):
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best_area:
                best_area = area
                best = h
    return best


class UIWindow:
    """Manages the pywebview window across the 4 UI states."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._current_state = "main"
        # §5.6: edge memory — expand returns toward the same edge
        self._last_edge = "top"
        # Remember main-state position to restore after undocking
        self._main_pos: tuple[int, int] | None = None
        self._edge_snap_enabled = True
        # Monotonic token for canceling stale animation threads: each
        # _animate_to call increments it; an in-flight thread aborts when its
        # captured token no longer matches the latest.
        self._anim_token: int = 0
        self._drag_token: int = 0   # captured at drag start; live_resize aborts once a settle bumps _anim_token
        # Size locked at begin_drag — live_move must never re-read w/h mid-drag
        # (a brief OS maximize / stale HWND rect would otherwise lock fullscreen).
        self._drag_size: tuple[int, int] | None = None
        # True while JS is edge-resizing main — live_resize must honor w/h.
        self._resizing: bool = False
        # Last user-chosen main size (session); restores after dock ↔ main.
        self._main_size: tuple[int, int] | None = None
        # Custom maximize (work-area fill) — not OS Aero maximize.
        self._maximized: bool = False
        self._pre_max_rect: tuple[int, int, int, int] | None = None  # x,y,w,h
        # HWND captured once at `shown` (when the webview is reliably
        # foreground) so later drag geometry never accidentally targets the
        # 启动.bat console window (which shares the title "AgentAssistant").
        self._cached_hwnd: int | None = None
        # Called with (state, is_snap) when a drag ends on a screen edge
        self.on_edge_snap: Callable[[str, bool], None] | None = None
        # Extra strip under docked-search when a confirm is waiting (JS-driven).
        self._docked_banner: bool = False

    @property
    def window(self) -> webview.Window | None:
        return self._window

    @property
    def current_state(self) -> str:
        return self._current_state

    def set_edge_snap_enabled(self, enabled: bool) -> None:
        self._edge_snap_enabled = enabled

    def state_size(self, state: str) -> tuple[int, int]:
        sw, sh = _screen_size()
        if state == "main" and self._main_size:
            # Clamp bumps stale narrow sizes left over from the old 40%-width main.
            return _clamp_main_size(self._main_size[0], self._main_size[1], sw, sh)
        w, h = state_size(state, (sw, sh))
        if state == "docked-search" and self._docked_banner:
            h = DOCKED_SEARCH_HEIGHT + DOCKED_SEARCH_BANNER_HEIGHT
        return w, h

    def set_docked_confirm_banner(self, visible: bool) -> None:
        """Grow/shrink docked-search to fit the confirm tip under the pill.

        Always syncs geometry when currently docked — do not early-return on
        unchanged flag (flag may have been set while not docked, then user
        docks and JS re-calls with the same True → previously skipped resize
        and the tip stayed clipped under the 48px pill).
        """
        self._docked_banner = bool(visible)
        if self._window is None or self._current_state != "docked-search":
            return
        try:
            x, y = int(self._window.x), int(self._window.y)
        except Exception:
            return
        w, h = self.state_size("docked-search")
        self._set_geometry(x, y, w, h)
        self._apply_win32_colorkey()
        self._disable_window_snap()
        self._apply_window_shape("docked-search")

    def start(self, initial_state: str = "main") -> None:
        """Create and show the webview window (blocking call)."""
        html_path = Path(__file__).parent / "web" / "index.html"

        if not html_path.exists():
            logger.error("UI files not found at %s", html_path)
            return

        self._current_state = initial_state
        w, h = self.state_size(initial_state)
        sw, _ = _screen_size()

        self._window = webview.create_window(
            title="AgentAssistant::UI",  # unique title for FindWindow — avoids
            # collision with the 启动.bat console which is also titled
            # "AgentAssistant" (that collision made _get_hwnd return the
            # console's HWND → drag geometry came from the console → the
            # window jumped erratically and false-snapped to a sliver).
            url=str(html_path),
            width=w,
            height=h,
            x=(sw - w) // 2,
            y=20,
            min_size=(12, 48),  # default (200,100) forces a white band
            resizable=False,
            fullscreen=False,
            # Must stay False — otherwise the assistant always stacks above
            # other apps and cannot be "clicked behind" (normal Windows focus).
            on_top=False,
            frameless=True,
            # easy_drag=True (default) installs a window-level mousedown→move
            # handler, so selecting chat text also drags the whole window.
            # We drag only via explicit JS handlers on title bars.
            easy_drag=False,
            # text_select=False (default) injects user-select:none globally,
            # blocking copy from chat bubbles.
            text_select=True,
            transparent=True,           # true window transparency (replaces brittle color-key)
            background_color="#000000",  # pywebview wants a 6-digit hex; ignored under transparent=True
            # Hidden until the page has fully loaded (see _show_when_loaded):
            # showing a transparent window before WebView2 paints anything
            # flashes a WHITE rectangle for seconds — worse on slow networks.
            hidden=True,
            js_api=api_bridge,
        )
        # transparent=True gives a truly transparent window (no black border).
        # The body must paint transparent in CSS so the desktop shows through;
        # only .glass elements draw a frosted surface.

        api_bridge.set_window(self._window)
        # Show ONLY after the page finished loading — first paint is then the
        # real UI, never the WebView2 default white surface.
        self._window.events.loaded += self._show_when_loaded
        #HWND capture / snap-strip / pill-shape work stays on `shown`.
        self._window.events.shown += self._ensure_visible
        # Safety net: if `loaded` never fires (broken page), show after 4s so
        # the app can't end up invisible — a white flash beats a dead app.
        threading.Timer(4.0, self._show_when_loaded).start()

        webview.start(debug=False)

    _shown_once = False

    def _show_when_loaded(self, *args) -> None:
        """Deferred display: show the window only once the page is painted."""
        if self._shown_once:
            return
        self._shown_once = True
        try:
            if self._window:
                self._window.show()
        except Exception:
            pass

    def _ensure_visible(self, *args) -> None:
        """Post-show work: capture the HWND, strip Aero Snap styles, re-cut the
        pill region. (The actual show() now happens in _show_when_loaded —
        showing on `shown` re-introduced the startup white flash.)"""
        # Capture the HWND via PID-based enumeration (deterministic — always
        # the webview, never the console). Cached forever after.
        if self._cached_hwnd is None and sys.platform == "win32":
            h = _find_self_hwnd()
            if h:
                self._cached_hwnd = h
        # Strip the resize/maximize styles so Windows Aero Snap can't grab this
        # frameless window (drag-to-top maximize / drag-to-side half-maximize)
        # — that OS-level snap was the 'drag the chat header → auto-fullscreen'
        # bug, which no amount of app-side logic can prevent otherwise.
        self._disable_window_snap()
        # FRAMECHANGED above can wipe a custom region — re-cut the pill if needed.
        self._apply_window_shape()

    def _disable_window_snap(self) -> None:
        """Strip styles that let Windows Aero Snap maximize this frameless window.

        Must be re-applied after every resize/move: Windows may restore
        ``WS_THICKFRAME | WS_MAXIMIZEBOX`` on geometry changes, which re-enables
        drag-to-top maximize (the 'drag chat header → fullscreen' bug).
        Do NOT early-return after a successful first call.
        """
        if sys.platform != "win32":
            return
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            user32 = ctypes.windll.user32
            # If Aero Snap already maximized us, restore before stripping styles.
            if user32.IsZoomed(hwnd):
                user32.ShowWindow(hwnd, _SW_RESTORE)
            style = user32.GetWindowLongW(hwnd, _GWL_STYLE)
            new_style = style & ~(_WS_THICKFRAME | _WS_MAXIMIZEBOX)
            if new_style != style:
                user32.SetWindowLongW(hwnd, _GWL_STYLE, new_style)
                # Frame redraw so the style change actually takes effect.
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    _SWP_FRAMECHANGED | _SWP_NOMOVE | _SWP_NOSIZE
                    | _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
        except Exception as e:
            logger.debug("disable_window_snap failed: %s", e)

    def _apply_win32_colorkey(self, *args) -> None:
        """No-op now that we use transparent=True (kept so existing call sites
        in set_state/_animate_to don't break). Color-key transparency was
        brittle (anti-aliased fringes + reset on resize → black border)."""
        return

    def _set_geometry(self, x: int, y: int, w: int, h: int) -> None:
        """Force window geometry via Win32 SetWindowPos (fallback: pywebview)."""
        if not self._window:
            return
        hwnd = self._get_hwnd()
        if hwnd and sys.platform == "win32":
            try:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, int(x), int(y), max(1, int(w)), max(1, int(h)),
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
                return
            except Exception as e:
                logger.debug("SetWindowPos geometry failed: %s", e)
        try:
            self._window.resize(max(1, int(w)), max(1, int(h)))
            self._window.move(int(x), int(y))
        except Exception as e:
            logger.debug("pywebview geometry failed: %s", e)

    def _apply_window_shape(self, state: str | None = None) -> None:
        """Pill-shaped HWND for docked-search; clear region for other states.

        Uses the *actual* Win32 window pixel size (GetWindowRect) — not the
        logical ``state_size`` — so DPI-scaled displays get a matching capsule.
        Must be called *after* ``_disable_window_snap`` / ``SetWindowPos``:
        ``SWP_FRAMECHANGED`` can clear a custom region.
        """
        if sys.platform != "win32":
            return
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        if state is None:
            state = self._current_state
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        try:
            if state == "docked-search":
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return
                w = max(1, int(rect.right - rect.left))
                h = max(1, int(rect.bottom - rect.top))
                # CreateRoundRectRgn right/bottom are exclusive.
                # Tall confirm banner → rounded card; plain pill → stadium.
                if self._docked_banner and h > DOCKED_SEARCH_HEIGHT + 4:
                    rad = min(DOCKED_SEARCH_HEIGHT, h // 2)
                else:
                    rad = h  # stadium / capsule
                rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, rad, rad)
                if rgn:
                    user32.SetWindowRgn(hwnd, rgn, True)
            else:
                user32.SetWindowRgn(hwnd, 0, True)
        except Exception as e:
            logger.debug("apply_window_shape(%s) failed: %s", state, e)

    # ─── State transitions (§5.1) ──────────────────────────────────

    def set_state(self, state: str, animate: bool = True, is_snap: bool = False) -> None:
        """Resize + reposition the window for the given state.

        When animate=True (default), the window morphs to the new size/position
        over ~400ms (easeOutCubic for size) for the Dynamic-Island feel. When
        is_snap=True a spring (easeOutBack) easing is applied to position for a
        more natural snap animation (~450ms). Use animate=False for instant
        programmatic changes (startup, settings).
        """
        if self._window is None:
            return
        # Legacy morphs collapse into the search pill.
        if state in ("minimized", "docked-sliver"):
            state = "docked-search"
        if state not in VALID_STATES:
            return

        # Banner only applies while docked; clear when expanding/closing pill.
        if state != "docked-search":
            self._docked_banner = False

        # Leaving main → remember position for restore
        if self._current_state == "main" and state != "main":
            try:
                self._main_pos = (self._window.x, self._window.y)
            except Exception:
                pass
            if self._maximized:
                self._maximized = False
                self._pre_max_rect = None
                self._notify_maximized(False)

        self._current_state = state
        w, h = self.state_size(state)
        sw, sh = _screen_size()
        hwnd = self._get_hwnd()
        wa_l, wa_t, wa_w, wa_h = _work_area_for_hwnd(hwnd)

        # Compute target position per state
        if state == "popup-chat":
            x = wa_l + (wa_w - w) // 2
            y = wa_t + DOCKED_SEARCH_TOP_GAP
        elif state == "docked-search":
            # Top-center of the current monitor's work area (taskbar-aware).
            x = wa_l + (wa_w - w) // 2
            y = wa_t + DOCKED_SEARCH_TOP_GAP
        else:  # main
            if self._main_pos:
                x = max(wa_l, min(self._main_pos[0], wa_l + wa_w - w))
                y = max(wa_t, min(self._main_pos[1], wa_t + wa_h - h))
            else:
                x = wa_l + (wa_w - w) // 2
                y = wa_t + max(20, (wa_h - h) // 3)

        if animate:
            if is_snap:
                self._animate_to(w, h, x, y, duration_ms=450, easing="spring")
            else:
                self._animate_to(w, h, x, y, duration_ms=400, easing="cubic")
        else:
            self._set_geometry(x, y, w, h)
            self._apply_win32_colorkey()
            self._disable_window_snap()
            self._apply_window_shape(state)

    def _animate_to(
        self,
        target_w: int,
        target_h: int,
        target_x: int,
        target_y: int,
        duration_ms: int = 350,
        easing: str = "cubic",
    ) -> None:
        """Morph the window to target size/position with easing.

        Runs on a daemon thread so it doesn't block the UI loop. A monotonic
        token cancels stale animations when a new ``_animate_to`` is started —
        the in-flight thread aborts as soon as its captured token no longer
        matches, preventing competing resize/move calls that made the window
        jump erratically on rapid state switches.

        Size always uses easeOutCubic (no overshoot — safe for resize).
        Position uses ``easing``: "cubic" (default) or "spring" (easeOutBack,
        a slight overshoot for a natural snap feel).

        Re-applies the Win32 color-key at the end (resize can reset layered
        attributes, which would show the #000001 body as a black border).
        """
        if not self._window:
            return
        try:
            start_w = self._window.width
            start_h = self._window.height
            start_x = self._window.x
            start_y = self._window.y
        except Exception:
            return

        sw, sh = _screen_size()

        import threading
        import time

        steps = max(10, duration_ms // 16)  # ~60fps

        # Increment token so any in-flight animation aborts on its next loop.
        self._anim_token += 1
        my_token = self._anim_token

        def run() -> None:
            for i in range(1, steps + 1):
                t = i / steps
                if self._anim_token != my_token:
                    return  # newer animation started — abort stale one
                e_size = 1 - (1 - t) ** 3  # easeOutCubic — always for size
                if easing == "spring":
                    # easeOutBack — slight overshoot, only for position
                    e_pos = 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2
                else:
                    e_pos = e_size
                w = max(1, int(start_w + (target_w - start_w) * e_size))
                h = max(1, int(start_h + (target_h - start_h) * e_size))
                x = max(0, min(int(start_x + (target_x - start_x) * e_pos), sw - w))
                y = max(0, min(int(start_y + (target_y - start_y) * e_pos), sh - h))
                try:
                    self._window.resize(w, h)
                    self._window.move(x, y)
                except Exception:
                    break
                time.sleep(duration_ms / steps / 1000)
            # Final pixel-perfect settle via Win32 (pywebview move can no-op).
            fx = max(0, min(target_x, sw - target_w))
            fy = max(0, min(target_y, sh - target_h))
            self._set_geometry(fx, fy, target_w, target_h)
            # Re-apply color-key after morph (fixes black border around glass)
            self._apply_win32_colorkey()
            # Re-strip resize/maximize styles — Windows may re-apply
            # _WS_THICKFRAME | _WS_MAXIMIZEBOX on resize, which re-enables
            # Aero Snap (drag-to-top maximize).
            self._disable_window_snap()
            self._apply_window_shape(self._current_state)

        threading.Thread(target=run, daemon=True, name="ui-morph").start()

    def begin_drag(self) -> dict:
        """Abort any in-flight settle, capture the token, return current rect.

        Bumping _anim_token cancels a running settle animation (e.g. user
        re-grabs within the ~400ms ease). live_resize then aborts once a
        later settle bumps the token again.
        """
        self._anim_token += 1
        self._drag_token = self._anim_token
        self._resizing = False
        # Windows-like: dragging a maximized window restores it to the
        # pre-max size first — do NOT discard _pre_max_rect without restoring
        # (that left the UI with no way to shrink back to the small main).
        if self._maximized:
            self._restore_from_maximize(notify=True)
        self._disable_window_snap()
        rect = self.get_rect()
        # Lock size for the whole drag — see live_move().
        self._drag_size = (max(1, int(rect.get("w") or 1)), max(1, int(rect.get("h") or 1)))
        return rect

    def begin_resize(self) -> dict:
        """Start an edge-resize gesture on main (size NOT locked)."""
        self._anim_token += 1
        self._drag_token = self._anim_token
        self._drag_size = None
        self._resizing = True
        if self._maximized:
            self._restore_from_maximize(notify=True)
        self._disable_window_snap()
        return self.get_rect()

    def is_maximized(self) -> bool:
        return bool(self._maximized)

    def _default_main_rect(self) -> tuple[int, int, int, int]:
        """Fallback (x, y, w, h) when pre-max geometry was lost."""
        hwnd = self._get_hwnd()
        wa_l, wa_t, wa_w, wa_h = _work_area_for_hwnd(hwnd)
        if self._main_size and not self._maximized:
            w, h = _clamp_main_size(self._main_size[0], self._main_size[1], wa_w, wa_h)
        else:
            w, h = state_size("main", (wa_w, wa_h))
        if self._main_pos:
            x = max(wa_l, min(self._main_pos[0], wa_l + wa_w - w))
            y = max(wa_t, min(self._main_pos[1], wa_t + wa_h - h))
        else:
            x = wa_l + (wa_w - w) // 2
            y = wa_t + max(20, (wa_h - h) // 3)
        return x, y, w, h

    def _restore_from_maximize(self, notify: bool = True) -> None:
        """Shrink from work-area fill back to a readable (not tiny) main window."""
        hwnd = self._get_hwnd()
        wa_l, wa_t, wa_w, wa_h = _work_area_for_hwnd(hwnd)
        if self._pre_max_rect:
            x, y, w, h = self._pre_max_rect
            w, h = _clamp_main_size(w, h, wa_w, wa_h)
            # If the saved rect was the old narrow main, bump to default comfort.
            dw, dh = state_size("main", (wa_w, wa_h))
            if w < dw * 0.85:
                w, h = dw, max(h, dh)
            x = max(wa_l, min(x, wa_l + wa_w - w))
            y = max(wa_t, min(y, wa_t + wa_h - h))
        else:
            x, y, w, h = self._default_main_rect()
        self._maximized = False
        self._pre_max_rect = None
        self._main_size = (w, h)
        self._main_pos = (x, y)
        self._set_geometry(x, y, w, h)
        self._disable_window_snap()
        self._apply_window_shape()
        if notify:
            self._notify_maximized(False)

    def _notify_maximized(self, maximized: bool) -> None:
        try:
            api_bridge.push_event("maximized", {"maximized": bool(maximized)})
        except Exception as e:
            logger.debug("notify maximized failed: %s", e)

    def toggle_maximize(self) -> bool:
        """Fill / restore the current monitor work area (frameless-safe).

        Avoids OS ``ShowWindow(SW_MAXIMIZE)`` so Aero Snap styles stay stripped.
        When already maximized, shrinks back to the previous (smaller) main size.
        """
        if not self._window or self._current_state != "main":
            return False
        if self._maximized:
            self._restore_from_maximize(notify=True)
            return False
        hwnd = self._get_hwnd()
        wa_l, wa_t, wa_w, wa_h = _work_area_for_hwnd(hwnd)
        rect = self.get_rect()
        self._pre_max_rect = (
            int(rect.get("x") or 0),
            int(rect.get("y") or 0),
            max(MAIN_MIN_SIZE[0], int(rect.get("w") or MAIN_MIN_SIZE[0])),
            max(MAIN_MIN_SIZE[1], int(rect.get("h") or MAIN_MIN_SIZE[1])),
        )
        self._maximized = True
        self._main_size = (wa_w, wa_h)
        self._main_pos = (wa_l, wa_t)
        self._set_geometry(wa_l, wa_t, wa_w, wa_h)
        self._disable_window_snap()
        self._apply_window_shape()
        self._notify_maximized(True)
        return True

    def live_move(self, x: int, y: int) -> None:
        """Instant move during a JS-tracked drag — size stays locked.

        Uses Win32 ``SetWindowPos`` with ``SWP_NOSIZE`` so we never call
        resize mid-drag (resize re-enables Aero Snap thick-frame styles, and
        re-reading ``get_rect()`` after a stray maximize would lock fullscreen).
        """
        if not self._window:
            return
        if self._anim_token != self._drag_token:
            return
        if self._drag_size:
            w, h = self._drag_size
        else:
            rect = self.get_rect()
            w, h = max(1, int(rect.get("w") or 1)), max(1, int(rect.get("h") or 1))
        sw, sh = _screen_size()
        w = max(1, min(w, sw))
        h = max(1, min(h, sh))
        x = max(0, min(int(x), sw - w))
        y = max(0, min(int(y), sh - h))
        hwnd = self._get_hwnd()
        if hwnd and sys.platform == "win32":
            try:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, 0, 0,
                    _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
                return
            except Exception as e:
                logger.debug("live_move SetWindowPos failed: %s", e)
        try:
            self._window.move(x, y)
        except Exception as e:
            logger.debug("live_move failed: %s", e)

    def live_resize(self, w: int, h: int, x: int, y: int) -> None:
        """Instant resize+move with no animation thread — live drag tracking.

        Called on every pointermove during a JS-tracked drag so the window
        follows the cursor in real time (smooth morph). Clamps to screen.
        Aborts once a settle animation has started (token advanced).

        When ``_drag_size`` is set (move drag), w/h are locked. During
        ``begin_resize`` that lock is cleared so the passed size is applied.
        """
        if not self._window:
            return
        if self._anim_token != self._drag_token:
            return  # settle animation started — abort stale live resize
        sw, sh = _screen_size()
        # Prefer drag-start size when set — callers may pass a stale/maximized
        # w/h after Aero Snap briefly fired; never adopt fullscreen mid-drag.
        if self._drag_size is not None and not self._resizing:
            w, h = self._drag_size
        min_w, min_h = (1, 1)
        if self._resizing and self._current_state == "main":
            min_w, min_h = MAIN_MIN_SIZE
        w = max(min_w, min(int(w), sw))
        h = max(min_h, min(int(h), sh))
        x = max(0, min(int(x), sw - w))
        y = max(0, min(int(y), sh - h))
        hwnd = self._get_hwnd()
        if hwnd and sys.platform == "win32":
            try:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, w, h,
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
                self._disable_window_snap()
                self._apply_window_shape()
                return
            except Exception as e:
                logger.debug("live_resize SetWindowPos failed: %s", e)
        try:
            self._window.resize(w, h)
            self._window.move(x, y)
        except Exception as e:
            logger.debug("live_resize failed: %s", e)
        self._disable_window_snap()
        self._apply_window_shape()

    def _win32_rect(self) -> tuple[int, int, int, int] | None:
        """Real window rect via Win32 GetWindowRect.

        pywebview's ``_window.x``/``.y`` can report 0 or a stale value on the
        transparent frameless window, which made ``begin_drag`` capture a wrong
        origin → ``live_move`` flung the window to the left edge →
        ``_check_edge_snap`` (which reads the *real* Win32 rect) saw
        ``left <= threshold`` and falsely snapped to docked-sliver — the
        'vertical blue line' that disappears. Reading geometry from the same
        Win32 source as the snap decision keeps the drag origin consistent.
        Returns (left, top, right, bottom) or None if unavailable.
        """
        if sys.platform != "win32":
            return None
        hwnd = self._get_hwnd()
        if not hwnd:
            return None
        import ctypes.wintypes as wintypes
        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def get_rect(self) -> dict:
        """Current window geometry {x, y, w, h} for JS drag math.

        Prefers the Win32 rect (same source ``_check_edge_snap`` uses) so the
        drag origin and the snap decision agree; falls back to pywebview's
        properties on non-Win32 or if the HWND can't be resolved.
        """
        r = self._win32_rect()
        if r:
            l, t, rr, b = r
            return {"x": l, "y": t, "w": rr - l, "h": b - t}
        if not self._window:
            return {"x": 0, "y": 0, "w": 0, "h": 0}
        try:
            return {
                "x": self._window.x,
                "y": self._window.y,
                "w": self._window.width,
                "h": self._window.height,
            }
        except Exception:
            return {"x": 0, "y": 0, "w": 0, "h": 0}

    def end_drag(self) -> None:
        """JS pointerup: release drag-size lock and re-strip Aero Snap styles."""
        if self._resizing and self._current_state == "main":
            r = self._win32_rect()
            if r:
                l, t, rr, b = r
                self._main_size = (
                    max(MAIN_MIN_SIZE[0], rr - l),
                    max(MAIN_MIN_SIZE[1], b - t),
                )
                self._main_pos = (l, t)
        self._drag_size = None
        self._resizing = False
        self._disable_window_snap()
        self._apply_window_shape()

    def check_edge_snap(self) -> None:
        """Public: run edge-snap detection after a JS-tracked drag.

        JS calls this on pointerup after moving the main window; if the
        window rests near a screen edge it snaps to docked-search/sliver.
        """
        self.end_drag()
        hwnd = self._get_hwnd()
        if hwnd:
            self._check_edge_snap(hwnd)

    def grow_to_main(self) -> None:
        """Animate to main size keeping current position (drag-grow settle).

        Used when the user drags the docked-search bar down past the
        threshold: the window has already grown live during the drag; this
        eases the remaining distance to full main size without repositioning
        (no jump to a saved/centered spot).
        """
        if not self._window:
            return
        try:
            x, y = self._window.x, self._window.y
        except Exception:
            return
        sw, sh = _screen_size()
        w, h = self.state_size("main")
        self._main_size = (w, h)
        self._current_state = "main"
        self._animate_to(w, h, max(0, min(x, sw - w)), max(0, min(y, sh - h)),
                         duration_ms=400, easing="cubic")

    def start_drag(self) -> None:
        """J6: Initiate window drag via Win32 WM_NCLBUTTONDOWN.

        SendMessage runs the modal drag loop — when it returns, the user
        released the mouse, so we immediately run edge-snap detection.
        """
        if sys.platform != "win32" or not self._window:
            return
        try:
            hwnd = self._get_hwnd()
            if not hwnd:
                return
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HT_CAPTION, 0)
            self._check_edge_snap(hwnd)
        except Exception as e:
            logger.debug("Drag failed: %s", e)

    def _check_edge_snap(self, hwnd: int) -> None:
        """§5.6: after a drag, snap main → docked when near a screen edge.

        Dragging a docked/minimized window away from its edge returns to main.
        Logic delegated to pure `detect_edge_snap` for testability.
        """
        try:
            import ctypes.wintypes as wintypes

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        except Exception:
            return

        sw, sh = _screen_size()
        rect_tuple = (rect.left, rect.top, rect.right, rect.bottom)
        result = detect_edge_snap(
            rect_tuple, (sw, sh), self._current_state, self._edge_snap_enabled,
            SNAP_THRESHOLD, RESTORE_THRESHOLD
        )
        if result is None:
            return
        new_state, edge = result
        if edge:
            self._last_edge = edge
        self._notify_snap(new_state)

    def _notify_snap(self, state: str) -> None:
        """Resize/move the window and tell the frontend to switch views."""
        self.set_state(state, is_snap=True)
        if self.on_edge_snap:
            try:
                self.on_edge_snap(state, True)
            except Exception as e:
                logger.debug("Edge-snap callback failed: %s", e)

    def set_click_through(self, enabled: bool) -> None:
        """F2: Toggle WS_EX_TRANSPARENT for click-through."""
        if sys.platform != "win32" or not self._window:
            return
        try:
            hwnd = self._get_hwnd()
            if not hwnd:
                return
            style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if enabled:
                style |= _WS_EX_TRANSPARENT | _WS_EX_LAYERED
            else:
                style &= ~_WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)
        except Exception as e:
            logger.debug("Click-through toggle failed: %s", e)

    def _get_hwnd(self) -> int | None:
        """Get the Win32 HWND for the webview window (robust multi-strategy).

        FindWindow by title is reliable when the title is unique; fall back to
        the foreground window (the assistant is foreground during user
        interaction). Returns None only if all strategies fail — in which case
        drag / color-key (black border) won't apply, so this MUST work.
        """
        if not self._window:
            return None
        # Preferred: the HWND found by PID enumeration (deterministic — always
        # the webview, never the console, no foreground races).
        if self._cached_hwnd:
            return self._cached_hwnd
        h = _find_self_hwnd()
        if h:
            self._cached_hwnd = h
            return h
        return None

    def show(self) -> None:
        if self._window:
            self._window.show()

    def hide(self) -> None:
        if self._window:
            self._window.hide()

    def destroy(self) -> None:
        if self._window:
            self._window.destroy()


# Singleton
ui_window = UIWindow()
