"""Tests for A4 memory system: token counter + memory manager."""

import json

from agent_assistant.memory.manager import MemoryManager, SnapshotSpec
from agent_assistant.memory.token_counter import count_messages_tokens, count_tokens


def _mock_summarizer(text: str) -> str:
    """Mock summarizer: truncate to first 100 chars (no LLM call)."""
    return f"Summary: {text[:100]}..."


def _test_snapshot_resolver(name: str) -> SnapshotSpec | None:
    """Registry-free resolver with the REAL tool-declared semantics.

    Mirrors what _registry_snapshot_resolver reads from the global registry
    (which is empty in unit tests — registration happens at app startup).
    """
    from agent_assistant.tools.ui_automation import ui_inspect_snapshot_key

    if name == "ui_inspect":
        return SnapshotSpec(key_fn=ui_inspect_snapshot_key, keep_recent=3)
    if name == "perf_snapshot":
        return SnapshotSpec(key_fn=lambda raw: raw or "", keep_recent=None)
    return None


# ─── Token Counter Tests ──────────────────────────────────────────────────────


class TestTokenCounter:
    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_simple_english(self):
        # "hello world" is ~2 tokens
        n = count_tokens("hello world")
        assert 1 <= n <= 5

    def test_chinese_text(self):
        n = count_tokens("你好世界，这是一个测试")
        assert n > 0

    def test_longer_text_more_tokens(self):
        short = count_tokens("hi")
        long = count_tokens("This is a much longer sentence with many words in it.")
        assert long > short

    def test_messages_tokens_includes_overhead(self):
        msgs = [{"role": "user", "content": "hello"}]
        n = count_messages_tokens(msgs)
        # Should be > raw content tokens due to per-message overhead
        raw = count_tokens("hello")
        assert n > raw

    def test_messages_tokens_empty_list(self):
        assert count_messages_tokens([]) == 0

    def test_messages_tokens_multiple(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        n = count_messages_tokens(msgs)
        assert n > 0

    def test_messages_with_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": '{"q":"test"}'}}
                ],
            }
        ]
        n = count_messages_tokens(msgs)
        assert n > 0


# ─── Memory Manager Tests ─────────────────────────────────────────────────────


def _make_messages(n_rounds: int, padding: str = "x" * 200) -> list[dict]:
    """Create fake conversation messages for testing."""
    msgs = [{"role": "system", "content": "You are a helpful assistant."}]
    for i in range(n_rounds):
        msgs.append({"role": "user", "content": f"Question {i}: {padding}"})
        msgs.append({"role": "assistant", "content": f"Answer {i}: {padding}"})
    return msgs


class TestMemoryManager:
    def setup_method(self):
        self.mgr = MemoryManager(
            token_budget=500,  # small budget to trigger truncation easily
            summary_cap=200,
            soft_rounds=4,
            summarizer=_mock_summarizer,
            snapshot_resolver=_test_snapshot_resolver,
        )

    def test_initial_state(self):
        assert self.mgr.rolling_summary == ""
        assert self.mgr.dropped_count == 0

    def test_no_truncation_under_budget(self):
        msgs = _make_messages(1, padding="short")
        result = self.mgr.maybe_compact(msgs)
        # Should not modify short conversation
        assert len(result) == len(msgs)
        assert self.mgr.rolling_summary == ""

    def test_truncation_over_budget(self):
        msgs = _make_messages(10, padding="word " * 100)
        result = self.mgr.maybe_compact(msgs)
        # Should have fewer messages than original
        assert len(result) < len(msgs)
        # System prompt always preserved
        assert result[0]["role"] == "system"
        # Most recent messages preserved
        assert result[-1]["role"] == "assistant"

    def test_rolling_summary_populated_after_compaction(self):
        msgs = _make_messages(10, padding="word " * 100)
        self.mgr.maybe_compact(msgs)
        # After compaction, rolling summary should be non-empty
        assert self.mgr.rolling_summary != ""

    def test_system_prompt_never_dropped(self):
        msgs = _make_messages(20, padding="word " * 200)
        result = self.mgr.maybe_compact(msgs)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == msgs[0]["content"]

    def test_recent_messages_preserved(self):
        msgs = _make_messages(10, padding="word " * 100)
        result = self.mgr.maybe_compact(msgs)
        # The last user+assistant pair should be intact
        last_content = msgs[-1]["content"]
        assert result[-1]["content"] == last_content

    def test_keeps_message_pairs_complete(self):
        """Never split a user/assistant pair."""
        msgs = _make_messages(10, padding="word " * 100)
        result = self.mgr.maybe_compact(msgs)
        # After system, messages should alternate user/assistant (or tool sequences)
        roles = [m["role"] for m in result[1:]]
        # First non-system should be user
        if roles:
            assert roles[0] == "user"

    def test_dropped_count_tracks(self):
        msgs = _make_messages(10, padding="word " * 100)
        self.mgr.maybe_compact(msgs)
        assert self.mgr.dropped_count > 0

    def test_summary_injected_into_context(self):
        msgs = _make_messages(10, padding="word " * 100)
        self.mgr.maybe_compact(msgs)
        context = self.mgr.build_context_prefix()
        assert "summary" in context.lower() or self.mgr.rolling_summary in context

    def test_idempotent_under_budget(self):
        """Calling maybe_compact twice on short msgs doesn't change them."""
        msgs = _make_messages(1, padding="hi")
        r1 = self.mgr.maybe_compact(msgs)
        r2 = self.mgr.maybe_compact(r1)
        assert r1 == r2

    def test_strip_incomplete_tool_group_at_tail(self):
        """Bare assistant(tool_calls) at the end must not stay in the keep window."""
        kept, stripped = MemoryManager._strip_incomplete_tool_groups([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "function": {"name": "perf_snapshot"}},
            ]},
        ])
        assert [m["role"] for m in kept] == ["user"]
        assert stripped[0].get("tool_calls")

    def test_keeps_complete_tool_group(self):
        kept, stripped = MemoryManager._strip_incomplete_tool_groups([
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "function": {"name": "perf_snapshot"}},
            ]},
            {"role": "tool", "tool_call_id": "x", "content": "{}"},
        ])
        assert stripped == []
        assert len(kept) == 3

    def test_inflight_turn_anchor_survives_and_middle_compacts(self):
        """A single-user-message agentic turn (user + N ×
        assistant(tool_calls)/tool) over budget keeps the anchor user
        instruction (task amnesia is unrecoverable) and the recent window,
        while the completed middle rounds are summarized away — otherwise
        hosted computer_task history grows unbounded (126K tokens observed)."""
        msgs = [{"role": "system", "content": "sys"}]
        msgs.append({"role": "user", "content": "帮我打开网易云播放夜曲 " + "x" * 300})
        for i in range(9):  # 9 tool rounds, well over budget=500
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"t{i}",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": f'{{"q": "{i}"}}'},
                }],
            })
            msgs.append({
                "role": "tool",
                "tool_call_id": f"t{i}",
                "content": '{"ok": true, "data": "' + "tree " * 100 + '"}',
            })

        result = self.mgr.maybe_compact(msgs)

        # Anchor user instruction always survives, right after system.
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert "帮我打开网易云播放夜曲" in result[1]["content"]
        # Middle rounds were dropped + summarized (no unbounded growth).
        assert len(result) < len(msgs)
        assert self.mgr.dropped_count > 0
        assert self.mgr.rolling_summary != ""
        # Pairing stays valid: nothing after the anchor starts on a bare tool.
        assert result[2]["role"] != "tool"
        # The most recent tool round is kept in full.
        assert result[-1]["role"] == "tool"
        assert result[-1]["tool_call_id"] == "t8"

    def test_inflight_turn_under_budget_untouched(self):
        """An in-flight agentic turn within budget is never modified."""
        msgs = [{"role": "system", "content": "sys"}]
        msgs.append({"role": "user", "content": "小任务"})
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "t0", "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": "t0", "content": "{}"})

        result = self.mgr.maybe_compact(msgs)
        assert result == msgs
        assert self.mgr.dropped_count == 0

    def test_compaction_drops_only_completed_turns(self):
        """Multi-turn: old completed turns may be summarized away, but the
        latest (in-flight) turn is always kept whole."""
        msgs = [{"role": "system", "content": "sys"}]
        # Old completed turn (user + assistant text answer), fat to force budget
        for i in range(6):
            msgs.append({"role": "user", "content": f"old q{i} " + "word " * 150})
            msgs.append({"role": "assistant", "content": f"old a{i} " + "word " * 150})
        # Current in-flight turn
        msgs.append({"role": "user", "content": "current task"})
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "cur0", "type": "function",
                "function": {"name": "web_search", "arguments": '{"q":"x"}'},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": "cur0", "content": "{}"})

        result = self.mgr.maybe_compact(msgs)

        assert result[0]["role"] == "system"
        # Current turn kept
        contents = [m.get("content") for m in result]
        assert "current task" in contents
        # No orphan tool message at the keep boundary
        assert result[1]["role"] != "tool"
        # Old turns were actually dropped + summarized
        assert self.mgr.dropped_count > 0
        assert self.mgr.rolling_summary != ""

    def test_keep_window_never_starts_on_tool_message(self):
        """If the split would land on a tool result, move forward so the
        kept window has no orphan tool message (API 400 otherwise)."""
        msgs = [{"role": "system", "content": "sys"}]
        msgs.append({"role": "user", "content": "q0 " + "word " * 200})
        for i in range(4):
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"t{i}", "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }],
            })
            msgs.append({
                "role": "tool", "tool_call_id": f"t{i}",
                "content": "out " * 200,
            })
        msgs.append({"role": "user", "content": "q1 follow-up"})
        msgs.append({"role": "assistant", "content": "a1"})

        result = self.mgr.maybe_compact(msgs)
        # Whatever remains, the first kept message is not a bare tool result
        if len(result) > 1:
            assert result[1]["role"] != "tool"

    # ─── Superseded snapshot stubbing ─────────────────────────────────────

    @staticmethod
    def _snapshot_turn(tool: str, args: str, payload: str, call_id: str) -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": tool, "arguments": args},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": payload},
        ]

    def test_superseded_snapshot_stubbed(self):
        """Second ui_inspect of the same window makes the first tree stale.

        Payloads sized so the post-stub conversation fits the 500-token
        budget — this test pins stubbing, not the drop path."""
        big_tree_1 = '{"ok": true, "data": "' + "treeA " * 100 + '"}'
        big_tree_2 = '{"ok": true, "data": "' + "treeB " * 100 + '"}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "网易云"}', big_tree_1, "c1")
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "网易云"}', big_tree_2, "c2")

        result = self.mgr.maybe_compact(msgs)

        # Older snapshot stubbed, newest kept in full
        assert "历史快照" in result[3]["content"]
        assert "treeA" not in result[3]["content"]
        assert result[5]["content"] == big_tree_2
        # Pairing + flow untouched: ids and assistant(tool_calls) intact
        assert result[3]["tool_call_id"] == "c1"
        assert result[2]["tool_calls"][0]["id"] == "c1"
        assert result[4]["tool_calls"][0]["function"]["name"] == "ui_inspect"

    def test_different_windows_not_stubbed(self):
        """Snapshots of DIFFERENT windows are both still informative."""
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "网易云"}', '{"a": 1}', "c1")
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "Cursor"}', '{"b": 2}', "c2")

        result = self.mgr.maybe_compact(msgs)
        assert result[3]["content"] == '{"a": 1}'
        assert result[5]["content"] == '{"b": 2}'

    def test_same_window_different_filters_kept(self):
        """Filtered ui_inspect must not wipe the last full tree of that window."""
        big_tree = '{"ok": true, "data": "' + "treeA " * 200 + '"}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn(
            "ui_inspect", '{"title_pattern": "网易云", "max_depth": 6}', big_tree, "c1")
        msgs += self._snapshot_turn(
            "ui_inspect", '{"title_pattern": "网易云", "control_type": "ListItem"}',
            '{"fresh": true}', "c2")

        result = self.mgr.maybe_compact(msgs)
        assert result[3]["content"] == big_tree
        assert result[5]["content"] == '{"fresh": true}'

    def test_empty_inspect_does_not_stub_full_tree(self):
        """count=0 must not become the 'latest' snapshot of the same key."""
        full = '{"ok": true, "data": {"count": 80, "controls": [' + '"x",' * 40 + '""]}}'
        empty = '{"ok": true, "data": {"count": 0, "controls": []}}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "大海"}', full, "c1")
        msgs += self._snapshot_turn("ui_inspect", '{"title_pattern": "大海"}', empty, "c2")

        result = self.mgr._stub_superseded_snapshots(msgs)
        assert "历史快照" not in result[3]["content"]
        assert result[3]["content"] == full
        assert result[5]["content"] == empty

    def test_stubbing_idempotent(self):
        """A second pass never rewrites already-stubbed payloads (they used to
        be re-stubbed every round → log spam + pointless list copies)."""
        big_1 = '{"ok": true, "data": "' + "t1 " * 200 + '"}'
        big_2 = '{"ok": true, "data": "' + "t2 " * 200 + '"}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", "{}", big_1, "c1")
        msgs += self._snapshot_turn("ui_inspect", "{}", big_2, "c2")

        first = self.mgr._stub_superseded_snapshots(msgs)
        assert "历史快照" in first[3]["content"]
        second = self.mgr._stub_superseded_snapshots(first)
        # No change on the second pass — same object list is returned.
        assert second is first
        assert second[3]["content"] == first[3]["content"]
        assert second[5]["content"] == big_2

    def test_oldest_inspect_stubbed_when_more_than_keep_limit(self):
        """Fourth distinct ui_inspect tree is stubbed; newest three stay."""
        trees = [
            '{"ok": true, "data": {"count": 2, "controls": ["' + f"t{i} " * 40 + '"]}}'
            for i in range(4)
        ]
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        for i, tree in enumerate(trees):
            msgs += self._snapshot_turn(
                "ui_inspect",
                f'{{"title_pattern": "w", "name_contains": "k{i}"}}',
                tree,
                f"c{i}",
            )
        result = self.mgr._stub_superseded_snapshots(msgs)
        # tool contents sit at odd indices after sys+user: 3,5,7,9
        assert "历史快照" in result[3]["content"]
        assert result[5]["content"] == trees[1]
        assert result[7]["content"] == trees[2]
        assert result[9]["content"] == trees[3]

    def test_slight_overshoot_skips_compaction(self):
        """Hysteresis: a few hundred tokens over budget must not summarizer."""
        from agent_assistant.memory.token_counter import count_messages_tokens

        mgr = MemoryManager(
            token_budget=200,
            summary_cap=200,
            soft_rounds=4,
            summarizer=_mock_summarizer,
        )
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        while count_messages_tokens(msgs[1:]) <= 200:
            msgs[-1]["content"] += " extra words to grow the window"
        conv = count_messages_tokens(msgs[1:])
        margin = max(400, 200 // 10)
        assert conv <= 200 + margin
        result = mgr.maybe_compact(msgs)
        assert len(result) == len(msgs)
        assert mgr.rolling_summary == ""

    def test_non_snapshot_tool_never_stubbed(self):
        """web_search results have independent value even with same args."""
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("web_search", '{"query": "夜曲"}', '{"r": 1}', "c1")
        msgs += self._snapshot_turn("web_search", '{"query": "夜曲"}', '{"r": 2}', "c2")

        result = self.mgr.maybe_compact(msgs)
        assert result[3]["content"] == '{"r": 1}'
        assert result[5]["content"] == '{"r": 2}'

    def test_stubbing_can_avoid_compaction(self):
        """When the budget overflow is mostly stale snapshots, stubbing alone
        brings the conversation back under budget — nothing gets dropped."""
        big_tree_1 = '{"ok": true, "data": "' + "treeA " * 300 + '"}'
        big_tree_2 = '{"ok": true, "data": "' + "treeB " * 100 + '"}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", "{}", big_tree_1, "c1")
        msgs += self._snapshot_turn("ui_inspect", "{}", big_tree_2, "c2")

        result = self.mgr.maybe_compact(msgs)

        # Nothing dropped: all 6 messages survive (one stubbed)
        assert len(result) == len(msgs)
        assert self.mgr.dropped_count == 0

    # ─── Argument age-off ─────────────────────────────────────────────────

    @staticmethod
    def _tool_group(call_id: str, args: str, tool: str = "write_file",
                    payload: str = '{"ok": true}') -> list[dict]:
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": tool, "arguments": args},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": payload},
        ]

    def test_old_group_args_shrunk_recent_kept(self):
        """6 write_file groups: the first 2 get long values cut, last 4 intact."""
        fat_args = json.dumps({"path": "a.txt", "content": "字" * 2000},
                              ensure_ascii=False)
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        for i in range(6):
            msgs += self._tool_group(f"c{i}", fat_args)

        changed = self.mgr._age_off_old_arguments(msgs)
        assert changed >= 1
        for i in range(2):
            args = json.loads(msgs[2 + i * 2]["tool_calls"][0]["function"]["arguments"])
            assert args["path"] == "a.txt"  # short value preserved verbatim
            assert args["content"].endswith("…[已截断]")
            assert len(args["content"]) < 220
        for i in range(4, 6):
            args = json.loads(msgs[2 + i * 2]["tool_calls"][0]["function"]["arguments"])
            assert args["content"] == "字" * 2000  # recent groups untouched

    def test_age_off_idempotent(self):
        fat_args = json.dumps({"content": "x" * 3000})
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(6):
            msgs += self._tool_group(f"c{i}", fat_args)
        self.mgr._age_off_old_arguments(msgs)
        snapshot = [json.dumps(m, ensure_ascii=False) for m in msgs]
        self.mgr._age_off_old_arguments(msgs)
        assert [json.dumps(m, ensure_ascii=False) for m in msgs] == snapshot

    def test_age_off_keeps_json_parseable_for_snapshot_keying(self):
        """Aged ui_inspect args must yield the SAME snapshot key as before."""
        from agent_assistant.tools.ui_automation import ui_inspect_snapshot_key

        fat = json.dumps({"title_pattern": "网易云", "name_contains": "播放",
                          "notes": "长" * 1500}, ensure_ascii=False)
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(6):
            msgs += self._tool_group(f"c{i}", fat, tool="ui_inspect")
        key_before = ui_inspect_snapshot_key(fat)
        self.mgr._age_off_old_arguments(msgs)
        aged = msgs[1]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(aged)  # still valid JSON
        assert parsed["title_pattern"] == "网易云"  # short filter intact
        assert parsed["notes"].endswith("…[已截断]")  # fat value shrunk
        assert ui_inspect_snapshot_key(aged) == key_before

    def test_age_off_skips_incomplete_groups(self):
        """A bare assistant(tool_calls) (no replies yet) is never aged."""
        fat_args = json.dumps({"content": "x" * 3000})
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(5):
            msgs += self._tool_group(f"c{i}", fat_args)
        msgs.append({
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "pending", "type": "function",
                "function": {"name": "write_file", "arguments": fat_args},
            }],
        })
        self.mgr._age_off_old_arguments(msgs)
        pending_args = msgs[-1]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(pending_args)["content"] == "x" * 3000

    def test_age_off_small_args_untouched(self):
        """Groups below the size floor keep their args byte-for-byte."""
        small = json.dumps({"path": "a.txt"})
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(6):
            msgs += self._tool_group(f"c{i}", small)
        before = [json.dumps(m, ensure_ascii=False) for m in msgs]
        assert self.mgr._age_off_old_arguments(msgs) == 0
        assert [json.dumps(m, ensure_ascii=False) for m in msgs] == before

    def test_pointer_stub_carries_call_id(self):
        """Superseded-snapshot stub now points at the archive via call_id."""
        big_1 = '{"ok": true, "data": "' + "t1 " * 200 + '"}'
        big_2 = '{"ok": true, "data": "' + "t2 " * 200 + '"}'
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "task"}]
        msgs += self._snapshot_turn("ui_inspect", "{}", big_1, "call_old1")
        msgs += self._snapshot_turn("ui_inspect", "{}", big_2, "call_new1")

        result = self.mgr._stub_superseded_snapshots(msgs)
        stub = result[3]["content"]
        assert "历史快照" in stub
        assert "call_old1" in stub
        assert "recall_tool_result" in stub
        json.loads(stub)  # still valid JSON (stubbed via json.dumps now)

    # ─── Declarative snapshot resolution ─────────────────────────────────

    def test_default_resolver_reads_registry_declarations(self):
        """_registry_snapshot_resolver maps Tool declarations → SnapshotSpec;
        unknown / non-snapshot tools resolve to None (never stubbed)."""
        from agent_assistant.memory.manager import _registry_snapshot_resolver
        from agent_assistant.tools import tool_registry
        from agent_assistant.tools.base import Tool, ToolResult

        class SnapProbe(Tool):
            name = "snap_probe"
            description = "snapshot probe"
            parameters = []

            @property
            def is_snapshot(self) -> bool:
                return True

            @property
            def snapshot_keep_recent(self) -> int | None:
                return 2

            def snapshot_key(self, raw_args: str) -> str:
                return "fixed-key"

            def execute(self, **kwargs):
                return ToolResult.success({})

        tool_registry.register(SnapProbe())
        try:
            spec = _registry_snapshot_resolver("snap_probe")
            assert spec is not None
            assert spec.keep_recent == 2
            assert spec.key_fn('{"a": 1}') == "fixed-key"
            assert _registry_snapshot_resolver("no_such_tool") is None
        finally:
            tool_registry.unregister("snap_probe")

    def test_non_snapshot_tool_declared_not_stubbed_via_registry(self):
        """A tool WITHOUT is_snapshot never enters the stub flow even when
        called twice with identical args (resolver-driven)."""
        from agent_assistant.tools import tool_registry
        from agent_assistant.tools.base import Tool, ToolResult

        class PlainProbe(Tool):
            name = "plain_probe"
            description = "plain tool"
            parameters = []

            def execute(self, **kwargs):
                return ToolResult.success({})

        tool_registry.register(PlainProbe())
        try:
            msgs = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "task"}]
            msgs += self._snapshot_turn("plain_probe", '{"q": "x"}', '{"r": 1}', "p1")
            msgs += self._snapshot_turn("plain_probe", '{"q": "x"}', '{"r": 2}', "p2")
            result = self.mgr._stub_superseded_snapshots(msgs)
            assert result[3]["content"] == '{"r": 1}'
            assert result[5]["content"] == '{"r": 2}'
        finally:
            tool_registry.unregister("plain_probe")

    # ─── Progress checkpoints ─────────────────────────────────────────────

    def test_checkpoint_covers_dropped_range_verbatim(self):
        """Messages before a dropped checkpoint are NOT re-summarized — the
        checkpoint text itself lands in the rolling summary; only gaps after
        it go through the summarizer."""
        calls: list[str] = []

        def counting_summarizer(text: str) -> str:
            calls.append(text)
            return f"GAP#{len(calls)}({text[:40]})"

        mgr = MemoryManager(
            token_budget=150,
            summary_cap=5000,
            soft_rounds=4,
            summarizer=counting_summarizer,
            snapshot_resolver=_test_snapshot_resolver,
        )
        cp = "【进度汇报】已完成打开应用和搜索，正在点播放。"
        mgr.record_checkpoint(cp)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "播放夜曲"},
            {"role": "assistant", "content": "step1 " + "x" * 3000},
            {"role": "assistant", "content": cp},
            {"role": "assistant", "content": "gap1 " + "y" * 3000},
            {"role": "assistant", "content": "gap2 " + "z" * 3000},
            {"role": "user", "content": "继续"},
        ]
        result = mgr.maybe_compact(msgs)

        # checkpoint merged VERBATIM (not through the summarizer)
        assert cp in mgr.rolling_summary
        # only the ONE post-checkpoint gap was summarized; the pre-checkpoint
        # rounds ([task, step1]) are covered by the report and never reach
        # the summarizer at all
        assert len(calls) == 1
        assert all(cp not in t for t in calls)
        assert "step1" not in calls[0]
        assert "gap1" in calls[0]
        assert "step1" not in mgr.rolling_summary  # covered by checkpoint
        assert "gap1" in mgr.rolling_summary  # gap present via its summary
        assert mgr.pending_checkpoints == []
        assert mgr.dropped_count == len(msgs) - len(result)
        # the recent window + anchor survive untouched
        assert result[-1]["content"] == "继续"

    def test_checkpoint_stays_pending_when_not_dropped(self):
        """A checkpoint whose message is still in the keep window is not
        consumed and not (yet) merged; all dropped content is summarized."""
        cp = "尚未被压缩的报告"
        self.mgr.record_checkpoint(cp)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old " + "a" * 4000},
            {"role": "assistant", "content": "old answer " + "b" * 4000},
            {"role": "user", "content": "new task"},
            {"role": "assistant", "content": cp},
        ]
        self.mgr.maybe_compact(msgs)
        assert self.mgr.dropped_count > 0
        assert self.mgr.pending_checkpoints == [cp]
        assert cp not in self.mgr.rolling_summary

    def test_summary_overflow_condenses_whole_summary(self):
        """When checkpoints + gaps exceed summary_cap, the WHOLE summary is
        condensed in one lossy step (the only place detail decays)."""

        def identity_summarizer(text: str) -> str:
            return text

        mgr = MemoryManager(
            token_budget=100,
            summary_cap=50,
            soft_rounds=4,
            summarizer=identity_summarizer,
            snapshot_resolver=_test_snapshot_resolver,
        )
        cp = "报告" * 200  # far over the 50-token cap by itself
        mgr.record_checkpoint(cp)
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task " + "w" * 3000},
            {"role": "assistant", "content": cp},
            {"role": "user", "content": "anchor"},
        ]
        mgr.maybe_compact(msgs)
        # identity summarizer → the condensed output IS the condense prompt
        assert mgr.rolling_summary.startswith("Condense this summary")
