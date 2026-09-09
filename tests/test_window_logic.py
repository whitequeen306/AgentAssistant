"""Tests for window state logic (pure functions, no Win32/pywebview).

Visual aspects (transparency, morph animation smoothness) are verified
manually by the user; these tests cover the deterministic logic only.
"""

from agent_assistant.ui.window import (
    decide_drag_settle,
    detect_edge_snap,
    drag_progress,
    drag_target_height,
    state_size,
)


# ─── state_size ────────────────────────────────────────────────

def test_state_size_main():
    # Main is a comfortable fixed default (not tied to the narrow search pill).
    assert state_size("main", (1920, 1080)) == (980, 640)


def test_state_size_main_clamps_to_small_screen():
    w, h = state_size("main", (900, 700))
    assert w <= 900 - 24
    assert h <= 700 - 48
    assert w >= 320


def test_state_size_legacy_minimized_maps_to_search():
    assert state_size("minimized", (1920, 1080)) == state_size("docked-search", (1920, 1080))


def test_state_size_legacy_sliver_maps_to_search():
    assert state_size("docked-sliver", (1920, 1080)) == state_size("docked-search", (1920, 1080))


def test_state_size_docked_search_narrow_bar():
    w, h = state_size("docked-search", (1920, 1080))
    assert w == 420  # narrow suspended pill, not a full-width slab
    assert h == 48  # just the bar — gap is real desktop above the window


def test_state_size_docked_search_other_resolution():
    w, h = state_size("docked-search", (2560, 1440))
    assert w == 420
    assert h == 48


def test_state_size_unknown_raises():
    import pytest

    with pytest.raises(ValueError):
        state_size("bogus", (1920, 1080))


def test_state_size_popup_chat():
    assert state_size("popup-chat", (1920, 1080)) == (480, 560)


def test_snap_popup_chat_does_not_trigger():
    assert detect_edge_snap((0, 0, 480, 560), (1920, 1080), "popup-chat", True) is None


# ─── detect_edge_snap ─────────────────────────────────────────
# Returns (new_state, edge) or None. edge ∈ {"top","bottom","left","right",None}.

def test_snap_main_near_top_no_snap():
    # Top snap removed: the search bar is a fixed "hanging" state entered
    # via a shrink button, not via dragging main to the top edge.
    assert detect_edge_snap((100, 0, 860, 560), (1920, 1080), "main", True) is None


def test_snap_main_near_bottom_no_snap():
    # bottom edge: no snap (search bar is top-only per §5.6)
    assert detect_edge_snap((100, 1000, 860, 1080), (1920, 1080), "main", True) is None


def test_snap_main_near_left_no_snap():
    # Main never edge-snaps (left/right sliver snap removed) — stays main.
    assert detect_edge_snap((0, 100, 760, 660), (1920, 1080), "main", True) is None


def test_snap_main_near_right_no_snap():
    assert detect_edge_snap((1160, 100, 1920, 660), (1920, 1080), "main", True) is None


def test_snap_main_center_no_snap():
    assert detect_edge_snap((500, 300, 1260, 860), (1920, 1080), "main", True) is None


def test_snap_disabled_even_at_edge():
    assert detect_edge_snap((100, 0, 860, 560), (1920, 1080), "main", False) is None


def test_snap_docked_dragged_to_center_returns_main():
    assert detect_edge_snap((500, 300, 700, 360), (1920, 1080), "docked-search", True) == ("main", None)


def test_snap_docked_still_at_edge_no_change():
    assert detect_edge_snap((100, 0, 1240, 64), (1920, 1080), "docked-search", True) is None


def test_snap_docked_restore_threshold():
    # docked-search dragged 30px from top edge — within the 60px restore
    # threshold, so it stays docked (hysteresis prevents jitter near edge)
    assert detect_edge_snap((500, 30, 700, 94), (1920, 1080), "docked-search", True) is None
    # docked-search dragged 70px from top edge — beyond the 60px restore
    # threshold → snaps back to main
    assert detect_edge_snap(
        (500, 70, 700, 134), (1920, 1080), "docked-search", True
    ) == ("main", None)


def test_snap_docked_restore_threshold_bottom():
    # docked-search near bottom edge (screen height=1080, threshold=60)
    # bottom=1050 → distance from bottom edge = 1080-1050 = 30 < 60 → stays
    assert detect_edge_snap((500, 986, 700, 1050), (1920, 1080), "docked-search", True) is None
    # bottom=1000 → distance from bottom edge = 1080-1000 = 80 > 60 → restore
    assert detect_edge_snap(
        (500, 936, 700, 1000), (1920, 1080), "docked-search", True
    ) == ("main", None)


def test_snap_docked_restore_threshold_left():
    # docked-search dragged off the left edge beyond restore threshold → main
    assert detect_edge_snap((30, 200, 450, 248), (1920, 1080), "docked-search", True) is None
    assert detect_edge_snap(
        (70, 200, 490, 248), (1920, 1080), "docked-search", True
    ) == ("main", None)


def test_snap_docked_restore_threshold_right():
    assert detect_edge_snap((1470, 200, 1890, 248), (1920, 1080), "docked-search", True) is None
    assert detect_edge_snap(
        (1430, 200, 1850, 248), (1920, 1080), "docked-search", True
    ) == ("main", None)


def test_snap_corner_no_snap_when_main():
    # corner (0,0): main never snaps now (top + left/right all removed)
    assert detect_edge_snap((0, 0, 760, 560), (1920, 1080), "main", True) is None


# ─── live drag morph (§5.6: search bar grows into main page) ─────

def test_drag_progress_zero_at_no_drag():
    assert drag_progress(0) == 0.0


def test_drag_progress_clamps_below_zero():
    assert drag_progress(-50) == 0.0


def test_drag_progress_full_at_max_drag():
    assert drag_progress(200) == 1.0


def test_drag_progress_clamps_above_max():
    assert drag_progress(300) == 1.0


def test_drag_progress_midpoint():
    assert drag_progress(100, 200) == 0.5


def test_drag_target_height_at_start():
    assert drag_target_height(0.0, 48, 560) == 48


def test_drag_target_height_at_full():
    assert drag_target_height(1.0, 48, 560) == 560


def test_drag_target_height_midpoint():
    assert drag_target_height(0.5, 48, 560) == 304


def test_decide_settle_docked_search_past_threshold():
    assert decide_drag_settle("docked-search", 150, threshold=100) == "main"


def test_decide_settle_docked_search_below_threshold():
    assert decide_drag_settle("docked-search", 50, threshold=100) == "docked-search"


def test_decide_settle_docked_search_at_threshold():
    assert decide_drag_settle("docked-search", 100, threshold=100) == "main"


def test_decide_settle_docked_search_negative_drag():
    assert decide_drag_settle("docked-search", -30, threshold=100) == "docked-search"


def test_decide_settle_main_unchanged():
    assert decide_drag_settle("main", 200, threshold=100) == "main"
