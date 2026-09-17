"""ProfileStore 的 token 上限行为。

画像存的是"稳定事实"，所以超限时该丢的是**陈旧的**，不是刚学到的。
"""

from __future__ import annotations

import pytest

from agent_assistant.memory.profile import ProfileStore


@pytest.fixture()
def store(tmp_path) -> ProfileStore:
    s = ProfileStore(db_path=tmp_path / "profile.db", token_cap=12)
    yield s
    s.close()


def test_insertion_order_is_preserved(tmp_path) -> None:
    """未超限时，条目按插入顺序（老 → 新）全部渲染。"""
    s = ProfileStore(db_path=tmp_path / "p.db", token_cap=500)   # 明确不触发
    try:
        for key in "abc":
            s.set(key, "v")
        text = s.render()
    finally:
        s.close()
    assert [line.split(":")[0].strip("- ") for line in text.splitlines()] == [
        "a", "b", "c",
    ]


def test_over_cap_drops_oldest_keeps_newest(tmp_path) -> None:
    """超限时丢最老、留最新——新事实比最初记录的更值钱。"""
    s = ProfileStore(db_path=tmp_path / "p.db", token_cap=12)
    try:
        for key in "abcde":          # a 最老，e 最新
            s.set(key, "x" * 20)
        text = s.render()
        assert "e" in text           # 最新的必须留下
        assert "a" not in text       # 最老的必须被丢
    finally:
        s.close()


def test_order_of_writes_not_reads_decides_age(tmp_path) -> None:
    """"新旧"由首次写入顺序决定，与读取顺序无关。"""
    s = ProfileStore(db_path=tmp_path / "p.db", token_cap=12)
    try:
        s.set("old_fact", "x" * 20)
        s.set("new_fact", "x" * 20)
        text = s.render()
        assert "new_fact" in text
        assert "old_fact" not in text
    finally:
        s.close()


def test_oversized_single_entry_still_rendered(tmp_path) -> None:
    """单条就超限也至少保留一条——空档案比超限档案更糟。"""
    s = ProfileStore(db_path=tmp_path / "p.db", token_cap=5)
    try:
        s.set("only", "y" * 400)
        text = s.render()
        assert "only" in text
    finally:
        s.close()


def test_empty_profile_renders_blank(store: ProfileStore) -> None:
    assert store.render() == ""
