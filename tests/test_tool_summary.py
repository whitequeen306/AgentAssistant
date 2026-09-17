"""工具结果摘要（零成本）与老结果 age-off 的测试。

对应 docs/context-management-refactor.md §6：老的工具结果被压成一行
分类摘要，完整原文留在 tool_archive（recall_tool_result 取回）。
"""

from __future__ import annotations

import json

from agent_assistant.memory.manager import MemoryManager
from agent_assistant.memory.tool_summary import summarize_tool_result, tool_category


def _mock_summarizer(text: str) -> str:  # noqa: ARG001
    return "SUMMARY"


class TestToolCategory:
    def test_known_categories(self):
        assert tool_category("read_file") == "file_read"
        assert tool_category("read_attached_source") == "file_read"
        assert tool_category("web_search") == "web_search"
        assert tool_category("read_page") == "web_read"
        assert tool_category("run_command") == "command"
        assert tool_category("ui_inspect") == "ui_snapshot"
        assert tool_category("ui_type") == "ui_action"
        assert tool_category("dispatch_research") == "subagent"

    def test_unknown_tool_falls_back(self):
        assert tool_category("nonexistent_tool") == "other"
        assert tool_category("") == "other"


class TestSummarizeSuccess:
    def test_web_search_lists_title_and_url(self):
        payload = json.dumps({
            "ok": True,
            "data": {"results": [
                {"title": "东北大学研招网", "url": "https://yz.neu.edu.cn/a"},
                {"title": "复试分数线", "url": "https://example.com/b"},
            ]},
        })
        out = summarize_tool_result("web_search", {"query": "东北大学"}, payload)
        assert out.startswith("ok=true |")
        assert "命中 2 条" in out
        assert "东北大学研招网" in out
        assert "https://yz.neu.edu.cn/a" in out

    def test_file_read_reports_path_and_lines(self):
        payload = json.dumps({
            "ok": True,
            "data": {"path": "src/loop.py", "content": "a\nb\nc"},
        })
        out = summarize_tool_result("read_file", {"path": "src/loop.py"}, payload)
        assert "读了 src/loop.py" in out
        assert "3 行" in out

    def test_file_read_long_path_keeps_tail_not_head(self):
        """绝对路径必须保留文件名 —— 截掉尾部等于丢了「读了哪个文件」。

        回归用例：早先直接用 _short(..., 60) 截断，八九十字符的绝对路径
        会变成 `C:\\Users\\hp\\Desktop\\AgentAssistant\\src\\agent_assistant\\memor…`，
        既看不出文件名，还把后面的「N 行」挤掉了。
        """
        deep = (
            r"C:\Users\hp\Desktop\AgentAssistant\src\agent_assistant"
            r"\memory\tool_summary.py"
        )
        out = summarize_tool_result(
            "read_file", {"path": deep},
            json.dumps({"ok": True, "data": {"path": deep, "content": "a\nb"}}),
        )
        assert out.endswith("2 行")
        assert "tool_summary.py" in out
        assert out.index("tool_summary.py") > out.index("…")

    def test_list_files_reports_entries_not_matches(self):
        """列目录不是「命中」——要报条数与目录/文件构成。"""
        payload = json.dumps({
            "ok": True,
            "data": {
                "path": "/proj",
                "entries": [
                    {"name": "src", "type": "dir"},
                    {"name": "tests", "type": "dir"},
                    {"name": "README.md", "type": "file"},
                ],
            },
        })
        out = summarize_tool_result("list_files", {"path": "/proj"}, payload)
        assert "列出 3 项" in out
        assert "2 目录" in out and "1 文件" in out
        assert "命中" not in out

    def test_command_reports_exit_code_and_command(self):
        payload = json.dumps({
            "ok": True,
            "data": {"exit_code": 0, "stdout": "l1\nl2"},
        })
        out = summarize_tool_result("run_command", {"command": "pytest -q"}, payload)
        assert "退出码 0" in out
        assert "pytest -q" in out

    def test_ui_snapshot_reports_window_and_control_count(self):
        payload = json.dumps({
            "ok": True,
            "data": {"title": "网易云音乐", "controls": [1, 2, 3]},
        })
        out = summarize_tool_result("ui_inspect", {}, payload)
        assert "窗口「网易云音乐」" in out
        assert "3 个控件" in out

    def test_arguments_used_when_result_is_thin(self):
        payload = json.dumps({"ok": True, "data": {}})
        out = summarize_tool_result("ui_type", {"text": "周杰伦夜曲"}, payload)
        assert "输入 周杰伦夜曲" in out


class TestSummarizeFailure:
    def test_category_preferred_over_raw_error(self):
        payload = json.dumps({
            "ok": False,
            "error": "HTTP 500 Internal Server Error",
            "error_category": "serp_fetch",
        })
        out = summarize_tool_result("read_page", {"url": "https://x"}, payload)
        assert out.startswith("ok=false |")
        assert "serp_fetch" in out
        assert "HTTP 500" not in out          # 写分类，不泄露原始报错

    def test_failure_without_category_still_bounded(self):
        payload = json.dumps({"ok": False, "error": "boom " * 200})
        out = summarize_tool_result("read_file", {"path": "a"}, payload)
        assert out.startswith("ok=false |")
        assert len(out) <= 200


class TestSummarizeRobustness:
    def test_unparseable_result_degrades_without_raising(self):
        out = summarize_tool_result("read_file", {"path": "a.txt"}, "not json at all")
        assert out.startswith("ok=")
        assert len(out) <= 200

    def test_empty_result_does_not_raise(self):
        for payload in ("", None, "{}", json.dumps({"ok": True})):
            out = summarize_tool_result("web_search", {}, payload)
            assert out.startswith("ok=")

    def test_summary_is_single_line_and_bounded(self):
        payload = json.dumps({"ok": True, "data": {"content": "x\ny\nz" * 500}})
        out = summarize_tool_result("read_file", {"path": "a"}, payload)
        assert "\n" not in out
        assert len(out) <= 200


class TestAgeOffOldResults:
    def _mgr(self) -> MemoryManager:
        # 预算要"装得下最近几条、装不下全部"，token 保护线才会真正生效。
        # （旧实现这里用 10**9 也没关系，因为当时按条数判定 —— 现在不行了。）
        return MemoryManager(
            token_budget=1200,
            summarizer=_mock_summarizer,
            snapshot_resolver=lambda _n: None,
        )

    @staticmethod
    def _msgs(rounds: int, *, blob_repeat: int = 200) -> list[dict]:
        """``blob_repeat`` 控制单条结果的体积，用于让累计越过/不越过保护线。"""
        msgs: list[dict] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "帮我查一下"},
        ]
        for i in range(rounds):
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"t{i}",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"q": str(i)}),
                    },
                }],
            })
            msgs.append({
                "role": "tool",
                "tool_call_id": f"t{i}",
                "content": json.dumps({
                    "ok": True,
                    "data": {"results": [
                        {"title": f"结果{i}", "url": f"https://e.com/{i}"},
                    ] + ["blob " * blob_repeat]},
                }),
            })
        return msgs

    @staticmethod
    def _tool_msgs(msgs: list[dict]) -> list[dict]:
        return [m for m in msgs if m.get("role") == "tool"]

    def test_old_summarized_recent_kept(self):
        """越过 token 保护线的老结果被压成一行，最近的保持原样。"""
        mgr = self._mgr()
        msgs = self._msgs(8)
        changed = mgr._age_off_old_groups(msgs)[0]
        assert changed > 0
        tools = self._tool_msgs(msgs)
        # 最老的必定被压，最新的必定保留（具体几条由 token 决定，不写死）
        assert "历史工具结果已压缩" in tools[0]["content"]
        assert "历史工具结果已压缩" not in tools[-1]["content"]
        # 被压的一定是时间上更早的那批（不会有"新的被压、旧的保留"）
        flags = ["历史工具结果已压缩" in m["content"] for m in tools]
        assert flags == sorted(flags, reverse=True)

    def test_summary_carries_recall_pointer(self):
        mgr = self._mgr()
        msgs = self._msgs(8)
        mgr._age_off_old_groups(msgs)[0]
        first = self._tool_msgs(msgs)[0]
        assert "recall_tool_result" in first["content"]
        assert "t0" in first["content"]

    def test_idempotent(self):
        mgr = self._mgr()
        msgs = self._msgs(8)
        assert mgr._age_off_old_groups(msgs)[0] > 0
        assert mgr._age_off_old_groups(msgs)[0] == 0   # 第二遍无变化

    def test_pairing_and_order_untouched(self):
        mgr = self._mgr()
        msgs = self._msgs(8)
        before = [(m.get("role"), m.get("tool_call_id")) for m in msgs]
        mgr._age_off_old_groups(msgs)[0]
        after = [(m.get("role"), m.get("tool_call_id")) for m in msgs]
        assert before == after
        assert len(msgs) == len(self._msgs(8))   # 消息数不变，只换 content

    def test_small_payload_untouched(self):
        """内容远小于 token 保护线 → 一条都不压。

        这正是"按 token 而非按条数"的意义：旧实现里只要组数够多就动手，
        于是 8 条几十字节的短命令也会被白白压掉；现在它们安全。
        """
        mgr = self._mgr()
        msgs = self._msgs(8, blob_repeat=2)
        assert mgr._age_off_old_groups(msgs)[0] == 0

    def test_action_threshold_blocks_tiny_gains(self):
        """可清理量太小则不动手 —— 避免为几 KB 每轮重写一遍消息。"""
        mgr = self._mgr()
        # 恰好越过保护线一点点：超出量够不上"保护线的 40%"这道门槛
        msgs = self._msgs(8, blob_repeat=3)
        assert mgr._age_off_old_groups(msgs)[0] == 0

    def test_summarized_content_stays_valid_json(self):
        mgr = self._mgr()
        msgs = self._msgs(8)
        mgr._age_off_old_groups(msgs)[0]
        for m in self._tool_msgs(msgs):
            parsed = json.loads(m["content"])   # 仍是合法 JSON（消息格式契约）
            assert "data" in parsed

    def test_incomplete_group_untouched(self):
        """最后一条 assistant(tool_calls) 还没结果 → 整组不动。"""
        mgr = self._mgr()
        msgs = self._msgs(8)
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "pending",
                "type": "function",
                "function": {"name": "web_search", "arguments": "{}"},
            }],
        })
        mgr._age_off_old_groups(msgs)[0]
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["tool_calls"][0]["id"] == "pending"
