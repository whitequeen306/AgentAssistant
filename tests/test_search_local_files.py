"""Bounded jail-aware local search (Task 13)."""

from __future__ import annotations

import os

import pytest

import agent_assistant.tools.search_local_files as slf
from agent_assistant.tools.search_local_files import SearchLocalFilesTool


@pytest.fixture()
def jail(tmp_path, monkeypatch):
    """Confine the file jail to tmp_path for every test."""
    monkeypatch.setattr(
        "agent_assistant.tools.permission.jail_roots", lambda: [str(tmp_path)]
    )
    return tmp_path


def run(**kwargs):
    return SearchLocalFilesTool().execute(**kwargs)


def test_name_search_finds_nested_files(jail) -> None:
    nested = jail / "docs" / "深处"
    nested.mkdir(parents=True)
    (nested / "考研资料汇总.pdf").write_bytes(b"%PDF-1.4 fake")
    (jail / "无关.txt").write_text("nothing", encoding="utf-8")

    result = run(root=str(jail), query="考研")
    assert result.ok is True
    assert result.data["count"] == 1
    hit = result.data["results"][0]
    assert hit["name"] == "考研资料汇总.pdf"
    assert hit["match"] == "name"
    assert os.path.isabs(hit["path"])
    assert hit["size"] > 0 and hit["modified"] > 0


def test_refuses_root_outside_jail(jail, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside-jail")
    result = run(root=str(outside), query="考研")
    assert result.ok is False
    assert result.error_category == "permission_denied"
    assert result.code == 403


def test_symlink_traversal_outside_root_is_skipped(jail, tmp_path_factory) -> None:
    secret_home = tmp_path_factory.mktemp("secret")
    (secret_home / "考研机密.txt").write_text("secret", encoding="utf-8")
    link = jail / "link-out"
    try:
        os.symlink(str(secret_home), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege on this machine")

    result = run(root=str(jail), query="考研")
    assert result.ok is True
    assert result.data["count"] == 0  # never followed the link


def test_binary_files_are_never_content_scanned(jail) -> None:
    binary = jail / "data.bin"
    binary.write_bytes(b"\x00\x01" + "考研".encode("utf-8") + b"\xff\xfe")
    exe = jail / "setup.exe"
    exe.write_bytes("考研".encode("utf-8"))

    result = run(root=str(jail), query="考研", search_content=True)
    # Neither .bin nor .exe are in the safe extension set.
    assert result.ok is True
    assert result.data["count"] == 0


def test_content_scan_finds_utf8_text_and_redacts_secrets(jail) -> None:
    note = jail / "笔记.md"
    note.write_text(
        "今年考研的政策变化很大。api_key=super-secret-value 不要外泄。",
        encoding="utf-8",
    )
    result = run(root=str(jail), query="政策变化", search_content=True)
    assert result.ok is True
    assert result.data["count"] == 1
    hit = result.data["results"][0]
    assert hit["match"] == "content"
    assert "政策变化" in hit["snippet"]
    assert "super-secret-value" not in hit["snippet"]
    assert len(hit["snippet"]) <= 260


def test_content_scan_respects_size_cap(jail, monkeypatch) -> None:
    big = jail / "big.txt"
    big.write_text("政策 " * 300000, encoding="utf-8")  # > 1 MiB
    result = run(root=str(jail), query="政策", search_content=True)
    assert result.ok is True
    assert result.data["count"] == 0


def test_non_utf8_text_file_is_skipped(jail) -> None:
    gbk = jail / "old.txt"
    gbk.write_bytes("考研政策".encode("gbk"))
    result = run(root=str(jail), query="政策", search_content=True)
    assert result.ok is True
    assert result.data["count"] == 0


def test_pattern_filter_and_result_cap(jail) -> None:
    for index in range(30):
        (jail / f"考研-{index}.txt").write_text("x", encoding="utf-8")
        (jail / f"考研-{index}.pdf").write_bytes(b"x")
    result = run(root=str(jail), query="考研", pattern="*.pdf", max_results=10)
    assert result.ok is True
    assert result.data["count"] == 10
    assert result.data["truncated"] is True
    assert all(hit["name"].endswith(".pdf") for hit in result.data["results"])
    assert result.warning is not None


def test_entry_budget_stops_search(jail, monkeypatch) -> None:
    for index in range(50):
        (jail / f"file-{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(slf, "_MAX_VISITED_ENTRIES", 5)
    result = run(root=str(jail), query="file")
    assert result.ok is True
    assert result.data["truncated"] is True
    assert result.data["visited_entries"] <= 6


def test_deadline_stops_search(jail, monkeypatch) -> None:
    (jail / "考研.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(slf, "_DEADLINE_SECONDS", -1.0)  # already expired
    result = run(root=str(jail), query="考研")
    assert result.ok is True
    assert result.data["truncated"] is True


def test_validation_failures(jail) -> None:
    missing_root = run(root="", query="x")
    missing_query = run(root=str(jail), query="  ")
    negative = run(root=str(jail), query="x", max_results=-5)
    too_big = run(root=str(jail), query="x", max_results=100000)
    boolean = run(root=str(jail), query="x", max_results=True)
    unknown_root = run(root=str(jail / "missing"), query="x")
    for result in (missing_root, missing_query, negative, too_big, boolean):
        assert result.ok is False
        assert result.code == 400
    assert unknown_root.ok is False
    assert unknown_root.code == 404


def test_root_file_rejected(jail) -> None:
    file_path = jail / "single.txt"
    file_path.write_text("x", encoding="utf-8")
    result = run(root=str(file_path), query="x")
    assert result.ok is False
    assert result.code == 400
