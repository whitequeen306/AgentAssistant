"""Tests for model-generated start chips (ui/chips.py + bridge caching)."""

import threading
from unittest.mock import MagicMock, PropertyMock, patch

from agent_assistant.ui.chips import _parse_chips, generate_smart_chips


class TestBridgeChipsCache:
    """generate_start_chips: key cache + in-flight dedup."""

    def _bridge(self):
        from agent_assistant.ui.bridge import ApiBridge

        return ApiBridge()

    def test_cache_hit_skips_llm(self, tmp_path):
        b = self._bridge()
        with (
            patch(
                "agent_assistant.ui.store.ui_store.get_setting", return_value="考研"
            ),
            patch(
                "agent_assistant.ui.chips.generate_smart_chips",
                return_value=[{"label": "l", "message": "m"}],
            ) as gen,
            patch(
                "agent_assistant.config.Settings.resolved_notes_dir",
                new_callable=PropertyMock,
                return_value=tmp_path,
            ),
            patch("agent_assistant.knowledge.service.knowledge_service") as ks,
        ):
            ks.list_files.return_value = []
            first = b.generate_start_chips()
            second = b.generate_start_chips()
        assert first["ok"] and first["chips"] == [{"label": "l", "message": "m"}]
        assert second == first
        assert gen.call_count == 1  # second call served from cache

    def test_concurrent_calls_share_one_llm_generation(self, tmp_path):
        b = self._bridge()
        gate = threading.Event()

        def slow_gen(*a, **k):
            gate.wait(timeout=5)
            return [{"label": "x", "draft": "深度调研：y"}]

        with (
            patch(
                "agent_assistant.ui.store.ui_store.get_setting", return_value="考研"
            ),
            patch(
                "agent_assistant.ui.chips.generate_smart_chips",
                side_effect=slow_gen,
            ) as gen,
            patch(
                "agent_assistant.config.Settings.resolved_notes_dir",
                new_callable=PropertyMock,
                return_value=tmp_path,
            ),
            patch("agent_assistant.knowledge.service.knowledge_service") as ks,
        ):
            ks.list_files.return_value = []
            results: list = []

            def call():
                results.append(b.generate_start_chips())

            t1 = threading.Thread(target=call)
            t1.start()
            import time as _t

            _t.sleep(0.1)  # let t1 become the owner
            t2 = threading.Thread(target=call)
            t2.start()
            gate.set()
            t1.join(timeout=10)
            t2.join(timeout=10)

        assert gen.call_count == 1  # piggybacked, not duplicated
        assert results[0]["chips"] == results[1]["chips"]


class TestParseChips:
    def test_valid_json(self):
        raw = (
            '{"chips": ['
            '{"label": "调研Agent框架", "draft": "深度调研：使用率较高的Agent开发框架"}, '
            '{"label": "制定复习计划", "message": "帮我制定408复习计划"}]}'
        )
        chips = _parse_chips(raw)
        assert chips == [
            {"label": "调研Agent框架", "draft": "深度调研：使用率较高的Agent开发框架"},
            {"label": "制定复习计划", "message": "帮我制定408复习计划"},
        ]

    def test_fenced_json(self):
        raw = '```json\n{"chips": [{"label": "a", "message": "b"}]}\n```'
        assert _parse_chips(raw) == [{"label": "a", "message": "b"}]

    def test_garbage_returns_empty(self):
        assert _parse_chips("not json at all") == []
        assert _parse_chips("") == []
        assert _parse_chips('{"chips": "nope"}') == []

    def test_drops_unlabeled_and_both_empty(self):
        raw = (
            '{"chips": ['
            '{"message": "no label"}, '
            '{"label": "both empty"}, '
            '{"label": "ok", "draft": "深度调研：x"}]}'
        )
        assert _parse_chips(raw) == [{"label": "ok", "draft": "深度调研：x"}]

    def test_cap_three_chips(self):
        items = ",".join(
            f'{{"label": "c{i}", "message": "m{i}"}}' for i in range(5)
        )
        assert len(_parse_chips('{"chips": [%s]}' % items)) == 3

    def test_long_values_truncated(self):
        raw = '{"chips": [{"label": "%s", "message": "%s"}]}' % ("x" * 60, "y" * 400)
        chips = _parse_chips(raw)
        assert len(chips[0]["label"]) <= 24
        assert len(chips[0]["message"]) <= 200


class TestGenerateSmartChips:
    def test_llm_failure_returns_empty(self):
        with patch("agent_assistant.llm.client.llm_client") as client:
            client.chat.side_effect = RuntimeError("no api key")
            assert generate_smart_chips({"goal": "考研"}, [], []) == []

    def test_success_passthrough(self):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = (
            '{"chips": [{"label": "l", "draft": "深度调研：d"}]}'
        )
        with patch("agent_assistant.llm.client.llm_client") as client:
            client.chat.return_value = resp
            chips = generate_smart_chips(
                {"major": "计算机", "goal": "求职"}, ["笔记A"], ["讲义B"]
            )
        assert chips == [{"label": "l", "draft": "深度调研：d"}]
        # Messages ground the model in profile + library.
        sent = client.chat.call_args.kwargs["messages"][1]["content"]
        assert "计算机" in sent and "求职" in sent
        assert "笔记A" in sent and "讲义B" in sent
