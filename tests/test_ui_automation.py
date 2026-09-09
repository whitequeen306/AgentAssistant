"""Unit tests for desktop UI automation (hotkey parse, driver helpers, tools)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_assistant.tools.ui_driver import (
    ControlSummary,
    UiDriver,
    parse_hotkey,
)
from agent_assistant.win_utils import WindowInfo


class TestParseHotkey:
    def test_ctrl_letter(self):
        assert parse_hotkey("ctrl+f") == "^f"

    def test_alt_enter(self):
        assert parse_hotkey("alt+enter") == "%{ENTER}"

    def test_enter_alone(self):
        assert parse_hotkey("enter") == "{ENTER}"

    def test_ctrl_shift_s(self):
        assert parse_hotkey("ctrl+shift+s") == "^+s"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_hotkey("")

    def test_win_rejected(self):
        with pytest.raises(ValueError, match="Win"):
            parse_hotkey("win+e")


class TestUiDriverInspectTruncate:
    def test_inspect_respects_max_nodes(self):
        driver = UiDriver()

        def fake_connect(_hwnd: int):
            root = MagicMock()
            root.element_info = SimpleNamespace(
                control_type="Window", name="App", automation_id=""
            )
            root.friendly_class_name.return_value = "Window"
            root.is_visible.return_value = True
            root.is_enabled.return_value = True

            children = []
            for i in range(20):
                child = MagicMock()
                child.element_info = SimpleNamespace(
                    control_type="Button",
                    name=f"Btn{i}",
                    automation_id=f"id{i}",
                )
                child.friendly_class_name.return_value = "Button"
                child.is_visible.return_value = True
                child.is_enabled.return_value = True
                child.children.return_value = []
                children.append(child)
            root.children.return_value = children
            return root

        with patch.object(driver, "connect", side_effect=fake_connect):
            nodes = driver.inspect(1, max_depth=2, max_nodes=5)

        assert len(nodes) == 5
        assert all(isinstance(n, ControlSummary) for n in nodes)
        assert nodes[0].name.startswith("Btn")


class TestUiDriverInspectFilter:
    def _make_driver_and_tree(self):
        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True
        root.children.return_value = []

        edit = MagicMock()
        edit.element_info = SimpleNamespace(
            control_type="Edit", name="搜索", automation_id="searchBox"
        )
        edit.friendly_class_name.return_value = "Edit"
        edit.is_visible.return_value = True
        edit.is_enabled.return_value = True
        edit.children.return_value = []

        item = MagicMock()
        item.element_info = SimpleNamespace(
            control_type="ListItem", name="夜曲 周杰伦", automation_id=""
        )
        item.friendly_class_name.return_value = "ListItem"
        item.is_visible.return_value = True
        item.is_enabled.return_value = True
        item.children.return_value = []

        root.children.return_value = [edit, item]
        return driver, root

    def test_filter_by_name_contains(self):
        driver, root = self._make_driver_and_tree()
        with patch.object(driver, "connect", return_value=root):
            nodes = driver.inspect(1, name_contains="夜曲")
        assert len(nodes) == 1
        assert nodes[0].control_type == "ListItem"
        assert "夜曲" in nodes[0].name

    def test_filter_by_control_type(self):
        driver, root = self._make_driver_and_tree()
        with patch.object(driver, "connect", return_value=root):
            nodes = driver.inspect(1, control_type="Edit")
        assert len(nodes) == 1
        assert nodes[0].control_type == "Edit"

    def test_includes_rect(self):
        driver, root = self._make_driver_and_tree()
        for child in root.children.return_value:
            child.rectangle.return_value = SimpleNamespace(
                left=110, top=240, width=lambda: 120, height=lambda: 36
            )
        with patch.object(driver, "connect", return_value=root):
            with patch.object(driver, "_window_rect", return_value=(100, 200)):
                nodes = driver.inspect(1)
        assert all(n.rect is not None for n in nodes)
        assert nodes[0].rect == (10, 40, 120, 36)  # relative to window


class TestSearchResultClick:
    def _rect(self, left, top, w, h):
        return SimpleNamespace(
            left=left, top=top, width=lambda: w, height=lambda: h
        )

    def test_name_contains_skips_search_edit_showing_query(self):
        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True

        edit = MagicMock()
        edit.element_info = SimpleNamespace(
            control_type="Edit", name="夜曲", automation_id="searchBox"
        )
        edit.friendly_class_name.return_value = "Edit"
        edit.is_visible.return_value = True
        edit.is_enabled.return_value = True
        edit.children.return_value = []
        edit.rectangle.return_value = self._rect(10, 8, 200, 28)

        song = MagicMock()
        song.element_info = SimpleNamespace(
            control_type="Text", name="夜曲", automation_id=""
        )
        song.friendly_class_name.return_value = "Text"
        song.is_visible.return_value = True
        song.is_enabled.return_value = True
        song.children.return_value = []
        song.rectangle.return_value = self._rect(20, 140, 180, 32)

        root.children.return_value = [edit, song]
        with patch.object(driver, "connect", return_value=root):
            with patch.object(driver, "_window_rect", return_value=(0, 0)):
                nodes = driver.inspect(1, name_contains="夜曲")
        assert [n.control_type for n in nodes] == ["Text"]
        assert nodes[0].name == "夜曲"
        assert nodes[0].actionable is True

    def test_click_by_name_prefers_text_row_over_search_edit(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [
                {
                    "id": "e0",
                    "name": "夜曲",
                    "auto_id": "searchBox",
                    "type": "Edit",
                    "rect": (10, 8, 200, 28),
                },
                {
                    "id": "e1",
                    "name": "夜曲",
                    "auto_id": "",
                    "type": "Text",
                    "rect": (20, 120, 180, 32),
                },
            ],
        )
        with patch.object(
            driver,
            "_click_point",
            return_value={"clicked": "(0,0)", "button": "left"},
        ) as click:
            with patch.object(driver, "_after_state", return_value=None):
                data = driver.click(1, target="夜曲")
        assert data["control"]["id"] == "e1"
        assert data["control"]["type"] == "Text"
        assert click.call_args[0][1:] == (110, 136, "left")

    def test_merge_keeps_edit_and_text_with_same_name(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [{
                "id": "e0",
                "name": "夜曲",
                "auto_id": "searchBox",
                "type": "Edit",
                "rect": (10, 8, 200, 28),
            }],
        )
        driver._merge_inspect_cache(1, [{
            "id": "tmp",
            "name": "夜曲",
            "auto_id": "",
            "type": "Text",
            "rect": (20, 120, 180, 32),
        }])
        cached = driver._inspect_cache[1][1]
        types = {e["type"] for e in cached}
        assert types == {"Edit", "Text"}

    def test_name_contains_finds_row_behind_visit_cap(self):
        """Sidebar Groups used to burn the 1500-visit cap before the song."""
        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True

        def _group():
            g = MagicMock()
            g.element_info = SimpleNamespace(
                control_type="Group", name="", automation_id=""
            )
            g.friendly_class_name.return_value = "Group"
            g.is_visible.return_value = True
            g.is_enabled.return_value = True
            g.children.return_value = []
            g.rectangle.return_value = self._rect(0, 0, 40, 40)
            return g

        song = MagicMock()
        song.element_info = SimpleNamespace(
            control_type="Text", name="夜曲", automation_id=""
        )
        song.friendly_class_name.return_value = "Text"
        song.is_visible.return_value = True
        song.is_enabled.return_value = True
        song.children.return_value = []
        song.rectangle.return_value = self._rect(20, 140, 180, 32)

        root.children.return_value = [_group() for _ in range(1600)] + [song]
        with patch.object(driver, "connect", return_value=root):
            with patch.object(driver, "_window_rect", return_value=(0, 0)):
                nodes = driver.inspect(1, name_contains="夜曲")
        assert any(n.control_type == "Text" and n.name == "夜曲" for n in nodes)

    def test_name_contains_walks_through_zero_size_parent(self):
        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True
        root.rectangle.return_value = self._rect(0, 0, 800, 600)

        parent = MagicMock()
        parent.element_info = SimpleNamespace(
            control_type="Group", name="", automation_id=""
        )
        parent.friendly_class_name.return_value = "Group"
        parent.is_visible.return_value = False
        parent.is_enabled.return_value = True
        parent.rectangle.return_value = self._rect(0, 0, 0, 0)

        song = MagicMock()
        song.element_info = SimpleNamespace(
            control_type="Text", name="夜曲", automation_id=""
        )
        song.friendly_class_name.return_value = "Text"
        song.is_visible.return_value = True
        song.is_enabled.return_value = True
        song.children.return_value = []
        song.rectangle.return_value = self._rect(20, 140, 180, 32)

        parent.children.return_value = [song]
        root.children.return_value = [parent]
        with patch.object(driver, "connect", return_value=root):
            with patch.object(driver, "_window_rect", return_value=(0, 0)):
                nodes = driver.inspect(1, name_contains="夜曲")
        assert [n.control_type for n in nodes] == ["Text"]
        assert nodes[0].name == "夜曲"

    def test_name_contains_findall_skips_sidebar_dfs(self):
        """UIA FindAll must return the song even when DFS never reaches it."""
        driver = UiDriver()
        root = MagicMock()
        song_info = SimpleNamespace(
            control_type="Text",
            name="夜曲",
            automation_id="",
            parent=None,
            enabled=True,
            rectangle=self._rect(20, 140, 180, 32),
        )
        edit_info = SimpleNamespace(
            control_type="Edit",
            name="夜曲",
            automation_id="searchBox",
            parent=None,
            enabled=True,
            rectangle=self._rect(10, 8, 200, 28),
        )

        def descendants(**kwargs):
            if kwargs.get("title") == "夜曲":
                return [edit_info, song_info]
            if kwargs.get("control_type") == "Text":
                return [song_info]
            if kwargs.get("control_type") == "Edit":
                return [edit_info]
            return []

        root.element_info = SimpleNamespace(
            control_type="Window",
            name="App",
            automation_id="",
            descendants=descendants,
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True
        root.children.return_value = []
        root.rectangle.return_value = self._rect(0, 0, 800, 600)

        with patch.object(driver, "connect", return_value=root):
            with patch.object(driver, "_window_rect", return_value=(0, 0)):
                nodes = driver.inspect(1, name_contains="夜曲")
        assert [n.control_type for n in nodes] == ["Text"]
        assert nodes[0].name == "夜曲"
        assert nodes[0].actionable is True


class TestUnfilteredInspectSkipsChromiumChrome:
    def _group(self, children):
        g = MagicMock()
        g.element_info = SimpleNamespace(
            control_type="Group", name="", automation_id=""
        )
        g.friendly_class_name.return_value = "Group"
        g.is_visible.return_value = True
        g.is_enabled.return_value = True
        g.children.return_value = children
        return g

    def test_unnamed_groups_do_not_consume_max_nodes(self):
        from agent_assistant.tools.ui_driver import keep_unfiltered_control

        assert keep_unfiltered_control("Group", "", "") is False
        assert keep_unfiltered_control("Edit", "", "searchBox") is True
        assert keep_unfiltered_control("Text", "夜曲", "") is True
        assert keep_unfiltered_control("Image", "logo", "") is False
        assert keep_unfiltered_control("Image", "sidebar_home", "") is False

        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True

        search = MagicMock()
        search.element_info = SimpleNamespace(
            control_type="Edit", name="搜索", automation_id="searchBox"
        )
        search.friendly_class_name.return_value = "Edit"
        search.is_visible.return_value = True
        search.is_enabled.return_value = True
        search.children.return_value = []

        song = MagicMock()
        song.element_info = SimpleNamespace(
            control_type="Text", name="夜曲", automation_id=""
        )
        song.friendly_class_name.return_value = "Text"
        song.is_visible.return_value = True
        song.is_enabled.return_value = True
        song.children.return_value = []

        inner = self._group([search, song])
        outer = self._group([inner])
        root.children.return_value = [outer]

        with patch.object(driver, "connect", return_value=root):
            nodes = driver.inspect(1, max_depth=6, max_nodes=5)

        names = [n.name for n in nodes]
        types = [n.control_type for n in nodes]
        assert "Group" not in types
        assert "搜索" in names
        assert "夜曲" in names
        assert driver.last_skipped_unnamed >= 2

    def test_filter_still_returns_matching_unnamed_if_requested(self):
        driver = UiDriver()
        root = MagicMock()
        root.element_info = SimpleNamespace(
            control_type="Window", name="App", automation_id=""
        )
        root.friendly_class_name.return_value = "Window"
        root.is_visible.return_value = True
        root.is_enabled.return_value = True
        g = self._group([])
        root.children.return_value = [g]
        with patch.object(driver, "connect", return_value=root):
            nodes = driver.inspect(1, control_type="Group")
        assert len(nodes) == 1
        assert nodes[0].control_type == "Group"


class TestPreferEditableEntry:
    def test_search_text_yields_nearby_edit(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [
                {
                    "id": "e0",
                    "name": "搜索",
                    "auto_id": "",
                    "type": "Text",
                    "rect": (10, 12, 40, 16),
                },
                {
                    "id": "e1",
                    "name": "搜索",
                    "auto_id": "searchBox",
                    "type": "Edit",
                    "rect": (10, 8, 200, 28),
                },
            ],
        )
        label = driver._cache_lookup(1, "e0")
        preferred = driver._prefer_editable_entry(1, label)
        assert preferred["id"] == "e1"
        assert preferred["type"] == "Edit"


class TestUiDriverTypeKeys:
    def test_cjk_falls_back_to_send_keys_not_clipboard(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [{
                "id": "e0",
                "name": "搜索",
                "auto_id": "searchBox",
                "type": "Edit",
                "rect": (10, 10, 80, 20),
            }],
        )
        with patch.object(driver, "_click_point"):
            with patch.object(driver, "_resolve_control", return_value=None):
                with patch("agent_assistant.tools.ui_driver.time.sleep"):
                    with patch("pywinauto.keyboard.send_keys") as send_keys:
                        data = driver.type_text(1, "夜曲", target="e0")
        assert data["via"] == "keys"
        assert data["typed"] == "夜曲"
        assert data["cleared"] is False
        sent = "".join(str(c.args[0]) for c in send_keys.call_args_list)
        assert "夜曲" in sent
        assert "^v" not in sent

    def test_clear_selects_all_then_types(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [{
                "id": "e0",
                "name": "搜索",
                "auto_id": "searchBox",
                "type": "Edit",
                "rect": (10, 10, 80, 20),
            }],
        )
        with patch.object(driver, "_click_point"):
            with patch.object(driver, "_resolve_control", return_value=None):
                with patch("agent_assistant.tools.ui_driver.time.sleep"):
                    with patch("pywinauto.keyboard.send_keys") as send_keys:
                        data = driver.type_text(1, "夜曲", target="e0", clear=True)
        assert data["via"] == "keys"
        assert data["cleared"] is True
        sent = [str(c.args[0]) for c in send_keys.call_args_list]
        assert any("^a" in s for s in sent)
        assert any("夜曲" in s for s in sent)
        assert not any("^v" in s for s in sent)

    def test_cjk_uses_value_pattern_when_edit_resolves(self):
        import time as time_mod

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time_mod.monotonic(),
            [{
                "id": "e0",
                "name": "搜索",
                "auto_id": "",
                "type": "Text",
                "rect": (10, 12, 40, 16),
            }, {
                "id": "e1",
                "name": "搜索",
                "auto_id": "searchBox",
                "type": "Edit",
                "rect": (10, 8, 200, 28),
            }],
        )
        ctrl = MagicMock()
        ctrl.set_edit_text = MagicMock()
        ctrl.get_value.return_value = "夜曲"
        ctrl.legacy_properties.side_effect = Exception("unused")
        with patch.object(driver, "_click_point"):
            with patch.object(driver, "_resolve_control", return_value=ctrl):
                with patch("agent_assistant.tools.ui_driver.time.sleep"):
                    with patch("pywinauto.keyboard.send_keys") as send_keys:
                        data = driver.type_text(1, "夜曲", target="e0")
        ctrl.set_edit_text.assert_called_once_with("夜曲")
        send_keys.assert_not_called()
        assert data["via"] == "value"
        assert data["landed"] is True
        assert data["field_value"] == "夜曲"
        assert data["control"]["id"] == "e1"


class TestUiDriverVisibilityFallback:
    def test_self_drawn_item_kept_if_has_rect(self):
        """Elements with is_visible=False but a valid rect should be kept."""
        from agent_assistant.tools.ui_driver import _is_visible

        ctrl = MagicMock()
        ctrl.is_visible.return_value = False
        ctrl.rectangle.return_value = SimpleNamespace(width=lambda: 100, height=lambda: 40)
        assert _is_visible(ctrl) is True

    def test_truly_hidden_dropped(self):
        from agent_assistant.tools.ui_driver import _is_visible

        ctrl = MagicMock()
        ctrl.is_visible.return_value = False
        ctrl.rectangle.return_value = SimpleNamespace(width=lambda: 0, height=lambda: 0)
        assert _is_visible(ctrl) is False


class TestUiDriverFindClick:
    def _ctrl(self, name: str, auto_id: str = "", ctype: str = "Button"):
        c = MagicMock()
        c.element_info = SimpleNamespace(
            control_type=ctype, name=name, automation_id=auto_id
        )
        c.is_visible.return_value = True
        c.is_enabled.return_value = True
        c.friendly_class_name.return_value = ctype
        return c

    def test_find_by_name(self):
        driver = UiDriver()
        search = self._ctrl("搜索", auto_id="searchBox", ctype="Edit")
        play = self._ctrl("播放")
        with patch.object(driver, "connect", return_value=MagicMock()):
            with patch.object(
                driver, "_iter_descendants", return_value=[search, play]
            ):
                found, peers = driver.find_control(1, "搜索")
        assert found is search
        assert "搜索" in peers

    def test_click_miss_lists_nearby(self):
        driver = UiDriver()
        a = self._ctrl("确定")
        b = self._ctrl("取消")
        with patch.object(driver, "connect", return_value=MagicMock()):
            with patch.object(driver, "_iter_descendants", return_value=[a, b]):
                with pytest.raises(LookupError, match="Nearby"):
                    driver.click(1, "不存在的按钮")


class TestUiTools:
    def test_inspect_success(self):
        from agent_assistant.tools.ui_automation import UiInspectTool

        win = WindowInfo(hwnd=42, title="记事本", pid=9)
        nodes = [
            ControlSummary("Edit", "文本编辑器", "", "Edit[0]", True, True),
        ]
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.inspect",
                return_value=nodes,
            ):
                result = UiInspectTool().execute()
        assert result.ok
        assert result.data["window"] == "记事本"
        assert result.data["count"] == 1

    def test_click_requires_target_or_xy(self):
        from agent_assistant.tools.ui_automation import UiClickTool

        result = UiClickTool().execute()
        assert not result.ok
        assert "target" in (result.error or "") or "x" in (result.error or "")

    def test_click_with_coordinates(self):
        from agent_assistant.tools.ui_automation import UiClickTool

        win = WindowInfo(hwnd=7, title="App", pid=1)
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.click",
                return_value={"clicked": "(120,300)", "button": "left", "coords": {"x": 120, "y": 300}},
            ) as mock_click:
                result = UiClickTool().execute(x=120, y=300)
        assert result.ok
        mock_click.assert_called_once_with(
            7, None, button="left", x=120, y=300, dx=0, dy=0
        )

    def test_click_with_target_and_offset(self):
        from agent_assistant.tools.ui_automation import UiClickTool

        win = WindowInfo(hwnd=7, title="App", pid=1)
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.click",
                return_value={"clicked": "(0,90)", "button": "left", "coords": {"x": 0, "y": 90}},
            ) as mock_click:
                result = UiClickTool().execute(target="搜索", dx=0, dy=90)
        assert result.ok
        mock_click.assert_called_once_with(
            7, "搜索", button="left", x=None, y=None, dx=0, dy=90
        )

    def test_scroll_success(self):
        from agent_assistant.tools.ui_automation import UiScrollTool

        win = WindowInfo(hwnd=9, title="Music", pid=3)
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.scroll",
                return_value={"scrolled": "down", "amount": 5, "target": "window_center", "coords": {"x": 400, "y": 300}},
            ) as mock_scroll:
                result = UiScrollTool().execute(direction="down", amount=5)
        assert result.ok
        mock_scroll.assert_called_once_with(9, direction="down", amount=5, target=None)

    def test_scroll_with_target(self):
        from agent_assistant.tools.ui_automation import UiScrollTool

        win = WindowInfo(hwnd=9, title="Music", pid=3)
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.scroll",
                return_value={"scrolled": "down", "amount": 3, "target": "歌曲列表", "coords": {"x": 200, "y": 400}},
            ) as mock_scroll:
                result = UiScrollTool().execute(direction="down", target="歌曲列表")
        assert result.ok
        mock_scroll.assert_called_once_with(9, direction="down", amount=3, target="歌曲列表")

    def test_descriptions_include_scroll(self):
        from agent_assistant.tools.ui_automation import UiScrollTool

        desc = UiScrollTool().description
        assert "When to call" in desc
        assert "When NOT" in desc
        assert "Suggested" in desc

    def test_hotkey_success(self):
        from agent_assistant.tools.ui_automation import UiHotkeyTool

        win = WindowInfo(hwnd=1, title="App", pid=2)
        with patch(
            "agent_assistant.tools.ui_automation.resolve_window", return_value=win
        ):
            with patch(
                "agent_assistant.tools.ui_automation.ui_driver.hotkey",
                return_value={"keys": "ctrl+f", "sent": "^f"},
            ) as mock_hk:
                result = UiHotkeyTool().execute(keys="ctrl+f")
        assert result.ok
        mock_hk.assert_called_once_with(1, "ctrl+f")

    def test_descriptions_have_chain_hints(self):
        from agent_assistant.tools.ui_automation import (
            UiClickTool,
            UiHotkeyTool,
            UiInspectTool,
            UiTypeTool,
        )

        for tool in (UiInspectTool(), UiClickTool(), UiTypeTool(), UiHotkeyTool()):
            desc = tool.description
            assert "When to call" in desc
            assert "When NOT" in desc
            assert "Suggested pattern" in desc or "Suggested" in desc


class TestPermissionAndHabits:
    def test_ui_permission_rules(self):
        from agent_assistant.tools.permission import (
            PermissionAction,
            default_permission_policy,
        )

        policy = default_permission_policy()
        assert policy.evaluate("ui_inspect", {}).action is PermissionAction.ALLOW
        assert policy.evaluate("ui_click", {"target": "x"}).action is PermissionAction.ASK
        assert policy.evaluate("ui_type", {"text": "a"}).action is PermissionAction.ASK
        assert policy.evaluate("ui_hotkey", {"keys": "enter"}).action is PermissionAction.ASK
        assert policy.evaluate("ui_scroll", {"direction": "down"}).action is PermissionAction.ASK

    def test_task_habits_mention_ui_tools(self):
        from agent_assistant.agent.prompt import SECTION_TASK_HABITS

        assert "ui_inspect" in SECTION_TASK_HABITS
        assert "ui_click" in SECTION_TASK_HABITS
        assert "ui_scroll" in SECTION_TASK_HABITS
        assert "音乐" in SECTION_TASK_HABITS or "siblings" in SECTION_TASK_HABITS

    def test_labels_registered(self):
        from agent_assistant.tools.labels import TOOL_LABELS_ZH, tool_label

        assert tool_label("ui_inspect") == "查看界面控件"
        assert "ui_hotkey" in TOOL_LABELS_ZH
        assert "ui_scroll" in TOOL_LABELS_ZH
        assert tool_label("ui_scroll") == "滚动界面"


class TestAssistantWindowGuard:
    def setup_method(self):
        from agent_assistant.tools.ui_driver import remember_ui_target

        remember_ui_target(None)

    def test_is_assistant_window(self):
        from agent_assistant.win_utils import is_assistant_window

        assert is_assistant_window(
            WindowInfo(hwnd=1, title="AgentAssistant::UI", pid=1)
        )
        assert is_assistant_window(WindowInfo(hwnd=2, title="AgentAssistant", pid=1))
        assert not is_assistant_window(
            WindowInfo(hwnd=3, title="大海 - 张雨生", pid=99)
        )

    def test_resolve_uses_last_target_when_foreground_is_self(self):
        from agent_assistant.tools.ui_driver import remember_ui_target, resolve_window

        self_win = WindowInfo(hwnd=1, title="AgentAssistant::UI", pid=1)
        app = WindowInfo(hwnd=2, title="大海 - 张雨生", pid=99)
        remember_ui_target(app)
        with patch(
            "agent_assistant.tools.ui_driver.get_foreground_window",
            return_value=self_win,
        ):
            with patch(
                "agent_assistant.tools.ui_driver.ctypes.windll.user32.IsWindow",
                return_value=True,
            ):
                got = resolve_window(None)
        assert got.hwnd == 2
        assert got.title.startswith("大海")

    def test_resolve_refuses_self_without_last_target(self):
        from agent_assistant.tools.ui_driver import resolve_window

        self_win = WindowInfo(hwnd=1, title="AgentAssistant::UI", pid=1)
        with patch(
            "agent_assistant.tools.ui_driver.get_foreground_window",
            return_value=self_win,
        ):
            with pytest.raises(LookupError, match="AgentAssistant"):
                resolve_window(None)

    def test_resolve_title_falls_back_to_exe(self):
        from agent_assistant.tools.ui_driver import resolve_window

        app_win = WindowInfo(hwnd=9, title="大海 - 张雨生", pid=28952)
        fake_app = SimpleNamespace(exe_path=r"D:\CloudMusic\cloudmusic.exe")
        with patch("agent_assistant.tools.ui_driver.find_windows", return_value=[]):
            with patch(
                "agent_assistant.tools.ui_driver.find_windows_by_exe",
                return_value=[app_win],
            ):
                with patch(
                    "agent_assistant.app_registry.app_registry.find_app",
                    return_value=fake_app,
                ):
                    with patch(
                        "agent_assistant.tools.ui_driver.focus_window_hwnd",
                        return_value=True,
                    ):
                        got = resolve_window("网易云音乐")
        assert got.title == "大海 - 张雨生"

    def test_filtered_inspect_keeps_previous_eids(self):
        import time

        from agent_assistant.tools.ui_driver import UiDriver

        driver = UiDriver()
        driver._inspect_cache[1] = (
            time.monotonic(),
            [
                {
                    "id": "e35",
                    "name": "search",
                    "auto_id": "",
                    "type": "Edit",
                    "rect": [10, 10, 80, 20],
                }
            ],
        )
        driver._merge_inspect_cache(
            1,
            [
                {
                    "id": "e0",
                    "name": "歌手：周杰伦",
                    "auto_id": "",
                    "type": "Text",
                    "rect": [50, 300, 100, 20],
                }
            ],
        )
        assert driver._cache_lookup(1, "e35") is not None
        assert driver._cache_lookup(1, "歌手：周杰伦") is not None


class TestAfterState:
    def test_reports_win32_foreground_without_uia_focus(self):
        driver = UiDriver()
        fg = WindowInfo(hwnd=42, title="网易云音乐", pid=7)
        with patch(
            "agent_assistant.tools.ui_driver.get_foreground_window",
            return_value=fg,
        ):
            info = driver._after_state(settle_s=0)
        assert info == {"foreground": "网易云音乐"}
