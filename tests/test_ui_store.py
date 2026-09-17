"""Tests for the UI SQLite store (docs/05 §5.2/5.4/5.5).

Covers conversations CRUD, message dedup (S3), scene trigger
matching (§5.4) and settings defaults/overrides (§5.5).
"""

import pytest

from agent_assistant.ui.store import DEFAULT_SETTINGS, UIStore


@pytest.fixture()
def store(tmp_path):
    s = UIStore(tmp_path / "ui.db")
    yield s
    s.close()


# ─── Conversations ────────────────────────────────────────────────────────────


class TestConversations:
    def test_create_and_list(self, store):
        conv = store.create_conversation()
        convs = store.list_conversations()
        assert len(convs) == 1
        assert convs[0]["id"] == conv["id"]
        assert convs[0]["pinned"] is False

    def test_purge_retired_report_conversations(self, store):
        # Removed briefing/perf features leave dedicated convs behind —
        # startup cleanup must remove them (with messages), keep the rest.
        a = store.create_conversation("晨间播报")
        b = store.create_conversation("性能检测")
        keep = store.create_conversation("正常会话")
        store.add_message(a["id"], "user", "report")
        store.add_message(keep["id"], "user", "hello")

        removed = store.purge_retired_report_conversations()
        assert removed == 2
        titles = {c["title"] for c in store.list_conversations()}
        assert titles == {"正常会话"}
        assert store.list_messages(keep["id"])
        # Idempotent: second pass removes nothing.
        assert store.purge_retired_report_conversations() == 0
        assert not store.list_messages(a["id"])
        assert not store.list_messages(b["id"])

    def test_title_from_first_message_truncated(self, store):
        conv = store.create_conversation()
        long_text = "x" * 100
        store.set_title_if_empty(conv["id"], long_text)
        title = store.list_conversations()[0]["title"]
        assert len(title) == 41  # 40 chars + ellipsis
        assert title.endswith("…")

    def test_title_not_overwritten(self, store):
        conv = store.create_conversation()
        store.set_title_if_empty(conv["id"], "first")
        store.set_title_if_empty(conv["id"], "second")
        assert store.list_conversations()[0]["title"] == "first"

    def test_rename(self, store):
        conv = store.create_conversation()
        store.rename_conversation(conv["id"], "renamed")
        assert store.list_conversations()[0]["title"] == "renamed"

    def test_pinned_sorts_first(self, store):
        a = store.create_conversation("a")
        b = store.create_conversation("b")
        store.set_pinned(a["id"], True)
        convs = store.list_conversations()
        assert convs[0]["id"] == a["id"]
        assert convs[0]["pinned"] is True
        assert convs[1]["id"] == b["id"]

    def test_search_filter(self, store):
        store.create_conversation("weather today")
        store.create_conversation("code review")
        results = store.list_conversations("weather")
        assert len(results) == 1
        assert results[0]["title"] == "weather today"

    def test_delete_removes_messages(self, store):
        conv = store.create_conversation()
        store.add_message(conv["id"], "user", "hi")
        store.delete_conversation(conv["id"])
        assert store.list_conversations() == []
        assert store.list_messages(conv["id"]) == []


# ─── Messages (S3 dedup) ──────────────────────────────────────────────────────


class TestMessages:
    def test_add_and_list(self, store):
        conv = store.create_conversation()
        store.add_message(conv["id"], "user", "hello")
        store.add_message(conv["id"], "assistant", "hi there")
        msgs = store.list_messages(conv["id"])
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "hello"

    def test_duplicate_id_ignored(self, store):
        """S3: same message id inserted twice → stored once."""
        conv = store.create_conversation()
        store.add_message(conv["id"], "user", "hello", msg_id="m1")
        store.add_message(conv["id"], "user", "hello again", msg_id="m1")
        msgs = store.list_messages(conv["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_auto_id_generated(self, store):
        conv = store.create_conversation()
        mid = store.add_message(conv["id"], "user", "x")
        assert mid
        assert store.list_messages(conv["id"])[0]["id"] == mid


# ─── Scenes (§5.4) ────────────────────────────────────────────────────────────


class TestScenes:
    def _game_scene(self, store):
        return store.save_scene({
            "name": "游戏模式",
            "trigger_phrases": ["游戏时间", "开逆战"],
            "actions": [{"tool": "launch_app", "args": {"name": "game"}}],
        })

    def test_save_and_get(self, store):
        scene = self._game_scene(store)
        assert scene["id"].startswith("scene-")
        loaded = store.get_scene(scene["id"])
        assert loaded["name"] == "游戏模式"
        assert loaded["trigger_phrases"] == ["游戏时间", "开逆战"]
        assert loaded["actions"][0]["tool"] == "launch_app"

    def test_upsert_updates(self, store):
        scene = self._game_scene(store)
        scene["name"] = "新名字"
        store.save_scene(scene)
        assert len(store.list_scenes()) == 1
        assert store.get_scene(scene["id"])["name"] == "新名字"

    def test_match_substring_case_insensitive(self, store):
        self._game_scene(store)
        assert store.match_scene("到游戏时间啦") is not None
        assert store.match_scene("现在 开逆战 吧") is not None
        assert store.match_scene("今天天气如何") is None

    def test_match_empty_text(self, store):
        self._game_scene(store)
        assert store.match_scene("") is None
        assert store.match_scene("   ") is None

    def test_delete(self, store):
        scene = self._game_scene(store)
        store.delete_scene(scene["id"])
        assert store.list_scenes() == []
        assert store.get_scene(scene["id"]) is None


# ─── Settings (§5.5) ──────────────────────────────────────────────────────────


class TestSettings:
    def test_defaults(self, store):
        assert store.get_setting("theme") == "system"
        assert store.get_setting("startup_state") == "main"
        assert store.get_setting("edge_snap") == "on"
        assert store.get_setting("reasoning_effort") == "low"

    def test_set_and_get(self, store):
        store.set_setting("theme", "dark")
        assert store.get_setting("theme") == "dark"

    def test_all_settings_overlay(self, store):
        store.set_setting("accent", "#FF0000")
        merged = store.all_settings()
        assert merged["accent"] == "#FF0000"
        # untouched keys keep defaults
        for key, value in DEFAULT_SETTINGS.items():
            if key != "accent":
                assert merged[key] == value

    def test_unknown_key_default(self, store):
        assert store.get_setting("nope", "fallback") == "fallback"


# ─── Find-or-create by title (perf report → dedicated conversation) ──────────


class TestFindOrCreate:
    def test_creates_when_absent(self, store):
        conv = store.find_or_create_conversation("性能检测")
        assert conv["title"] == "性能检测"
        assert conv["id"]
        assert any(c["title"] == "性能检测" for c in store.list_conversations())

    def test_returns_existing_not_duplicate(self, store):
        first = store.find_or_create_conversation("性能检测")
        second = store.find_or_create_conversation("性能检测")
        assert first["id"] == second["id"]
        matches = [c for c in store.list_conversations() if c["title"] == "性能检测"]
        assert len(matches) == 1

    def test_distinct_titles_distinct_convs(self, store):
        a = store.find_or_create_conversation("性能检测")
        b = store.find_or_create_conversation("晨间播报")
        assert a["id"] != b["id"]

    def test_exact_match_only_not_partial(self, store):
        # A conv titled "性能检测报告" must NOT satisfy a find for "性能检测"
        store.create_conversation("性能检测报告")
        found = store.find_or_create_conversation("性能检测")
        assert found["title"] == "性能检测"
        matches = [c for c in store.list_conversations() if c["title"] == "性能检测"]
        assert len(matches) == 1  # the exact one, not the partial-named one
