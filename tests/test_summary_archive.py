"""摘要归档（拼接策略的溢出处理）与 recall_summary 的测试。

设计要点见 docs/context-management-refactor.md §7：
摘要超限时**归档最老层 + 留指针**，绝不"再压一遍"（那正是要防的失真）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_assistant.config import settings
from agent_assistant.memory import summary_archive
from agent_assistant.memory.manager import MemoryManager
from agent_assistant.tools.recall_summary import RecallSummaryTool


@pytest.fixture()
def archive_dir(tmp_path: Path, monkeypatch):
    """把 data_dir 指到临时目录（无执行上下文时归档落到 sessions/local/）。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path / "sessions" / "local" / "summaries"


def _mgr(summary_cap: int, summarizer=None) -> MemoryManager:
    return MemoryManager(
        token_budget=10**9,          # 本组测试只关心摘要层，不触发丢弃
        summary_cap=summary_cap,
        summarizer=summarizer or (lambda text: text),
        snapshot_resolver=lambda _n: None,
    )


class TestSummaryArchive:
    def test_archive_and_read_roundtrip(self, archive_dir):
        files = summary_archive.archive_layers(
            ["第一层内容", "第二层内容"], stamp="20260914-120000"
        )
        assert len(files) == 2
        assert summary_archive.read_summary(files[0]) == "第一层内容"
        assert summary_archive.read_summary(files[1]) == "第二层内容"

    def test_list_carries_excerpt(self, archive_dir):
        summary_archive.archive_layers(
            ["用户要求比较两所学校的分数线"], stamp="20260914-120001"
        )
        listed = summary_archive.list_summaries()
        assert len(listed) == 1
        assert "用户要求比较两所学校的分数线" in str(listed[0]["excerpt"])

    def test_search_matches_keyword(self, archive_dir):
        summary_archive.archive_layers(
            ["关于东北大学的分数线调研"], stamp="20260914-120002"
        )
        hits = summary_archive.search_summaries("东北大学")
        assert len(hits) == 1
        assert "东北大学" in str(hits[0]["snippet"])

    def test_search_miss_is_empty(self, archive_dir):
        summary_archive.archive_layers(["无关内容"], stamp="20260914-120003")
        assert summary_archive.search_summaries("不存在的词") == []

    def test_read_rejects_path_traversal(self, archive_dir):
        assert summary_archive.read_summary("../../secret.md") == ""
        assert summary_archive.read_summary("nope.md") == ""

    def test_archive_failure_never_raises(self, tmp_path, monkeypatch):
        """归档是辅助能力：目录不可写时也不能抛异常打断对话。"""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")   # 文件占位 → mkdir 失败
        monkeypatch.setattr(settings, "data_dir", blocker)
        assert summary_archive.archive_layers(["某层"]) == []

    def test_split_layers_roundtrip(self):
        layers = summary_archive.split_layers("A\n\n---\n\nB\n\n---\n\nC")
        assert layers == ["A", "B", "C"]

    def test_pointer_line_mentions_recall(self):
        line = summary_archive.pointer_line(["a.md", "b.md"])
        assert "a.md" in line
        assert "recall_summary" in line

    def test_slug_skips_section_headings(self, archive_dir):
        """schema 化摘要层的首行是「## 目标」——文件名索引必须取首个内容行，
        否则所有归档都叫「…-目标.md」，索引退化。"""
        files = summary_archive.archive_layers(
            ["## 目标\n用户要求整理ML课件并标注考试范围\n## 已完成\n读了三份课件"],
            stamp="20260914-120004",
        )
        assert "用户要求整理ML课件" in files[0]
        assert files[0].split("-")[-1] != "目标.md"

    def test_sessions_are_isolated(self, archive_dir):
        """归档必须按会话隔离：A 会话归档的层，B 会话列不出也读不到。"""
        summary_archive.archive_layers(
            ["会话A的摘要层"], conversation_id="conv-a", stamp="20260914-120005"
        )
        summary_archive.archive_layers(
            ["会话B的摘要层"], conversation_id="conv-b", stamp="20260914-120006"
        )

        listed_a = summary_archive.list_summaries(conversation_id="conv-a")
        listed_b = summary_archive.list_summaries(conversation_id="conv-b")
        assert len(listed_a) == 1 and len(listed_b) == 1
        assert "会话A" in str(listed_a[0]["excerpt"])
        assert "会话B" in str(listed_b[0]["excerpt"])

        # B 拿 A 的文件名也读不到（跨会话不泄漏）
        name_a = str(listed_a[0]["name"])
        assert summary_archive.read_summary(name_a, conversation_id="conv-b") == ""
        assert summary_archive.read_summary(name_a, conversation_id="conv-a") != ""
        # 搜索同样只在当前会话内命中
        assert summary_archive.search_summaries("会话A", conversation_id="conv-b") == []


class TestFitWithinCap:
    def test_archives_oldest_layers_and_leaves_pointer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        mgr = _mgr(summary_cap=60)
        combined = "\n\n---\n\n".join([
            "层一" + "a" * 200,
            "层二" + "b" * 200,
            "层三" + "c" * 200,
        ])

        out = mgr._fit_within_cap(combined)

        # 指针行在场（否则模型不知道归档存在）
        assert "recall_summary" in out
        # 最新层完整保留，最老层不再出现在上下文里
        assert "c" * 100 in out
        assert "a" * 100 not in out
        # 最老层确实进了归档（内容可从磁盘取回）
        archived = summary_archive.list_summaries()
        assert archived, "expected at least one archived layer"
        stored = summary_archive.read_summary(str(archived[0]["name"]))
        assert "a" * 100 in stored or "b" * 100 in stored

    def test_single_oversized_layer_is_still_condensed(self, tmp_path, monkeypatch):
        """单层超限时没有"更老的层"可归档，此时才允许摘要器介入。"""
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        calls: list[str] = []

        def recording(text: str) -> str:
            calls.append(text)
            return "CONDENSED"

        mgr = _mgr(summary_cap=20, summarizer=recording)
        out = mgr._fit_within_cap("单层" + "x" * 500)

        assert out == "CONDENSED"
        assert calls and calls[0].startswith("Condense this summary")

    def test_under_cap_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        mgr = _mgr(summary_cap=10**6)
        combined = "A\n\n---\n\nB"
        assert mgr._fit_within_cap(combined) == combined


class TestRecallSummaryTool:
    def test_list_when_no_arguments(self, archive_dir):
        summary_archive.archive_layers(["某层"], stamp="20260914-130000")
        res = RecallSummaryTool().execute()
        assert res.ok
        assert res.data["archived"]

    def test_read_by_name(self, archive_dir):
        files = summary_archive.archive_layers(
            ["具体内容 abc"], stamp="20260914-130001"
        )
        res = RecallSummaryTool().execute(name=files[0])
        assert res.ok
        assert "具体内容 abc" in res.data["text"]

    def test_search_by_keyword(self, archive_dir):
        summary_archive.archive_layers(
            ["含有关键词 zebra 的一层"], stamp="20260914-130002"
        )
        res = RecallSummaryTool().execute(keyword="zebra")
        assert res.ok
        assert res.data["results"]

    def test_unknown_name_fails_cleanly(self, archive_dir):
        res = RecallSummaryTool().execute(name="missing.md")
        assert not res.ok
        assert res.error_category == "not_found"
