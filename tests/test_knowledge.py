"""Knowledge base ingest / registry / context attachment."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_assistant.context_attachment import (
    format_attachment_hint,
    parse_context_attachment,
)
from agent_assistant.knowledge.ingest import clean_text, safe_filename, validate_source_path
from agent_assistant.knowledge.registry import KnowledgeFile, KnowledgeRegistry
from agent_assistant.knowledge.service import KnowledgeService


def test_clean_text_normalizes():
    raw = "a\r\nb\x00c\n\n\n\nd"
    assert clean_text(raw) == "a\nbc\n\nd"


def test_safe_filename():
    assert "/" not in safe_filename("../../etc/passwd")
    assert safe_filename("报告 初稿.md").endswith(".md") or "报告" in safe_filename(
        "报告 初稿.md"
    )


def test_validate_source_path(tmp_path: Path):
    good = tmp_path / "a.md"
    good.write_text("hello", encoding="utf-8")
    ok, _ = validate_source_path(good)
    assert ok

    bad = tmp_path / "a.exe"
    bad.write_bytes(b"x")
    ok, err = validate_source_path(bad)
    assert not ok
    assert "unsupported" in err


def test_registry_roundtrip(tmp_path: Path):
    reg = KnowledgeRegistry(tmp_path / "files.json")
    entry = KnowledgeFile(
        file_id="abc123",
        title="T",
        filename="t.md",
        stored_path=str(tmp_path / "t.md"),
        size=10,
        chunk_count=2,
    )
    reg.add(entry)
    reg2 = KnowledgeRegistry(tmp_path / "files.json")
    assert reg2.get("abc123") is not None
    assert reg2.get("abc123").title == "T"
    removed = reg2.remove("abc123")
    assert removed is not None
    assert reg2.get("abc123") is None


def test_parse_context_attachment_exclusive():
    att = parse_context_attachment(
        {
            "primary": "knowledge",
            "notes": [{"kind": "note", "path": "a.md", "title": "A"}],
            "files": [{"kind": "file", "path": "C:/x.txt", "title": "x"}],
        }
    )
    assert att.primary == "knowledge"
    assert att.notes == []  # cleared when knowledge
    assert len(att.files) == 1
    assert att.use_knowledge
    hint = format_attachment_hint(att)
    assert "knowledge base" in hint.lower() or "search_knowledge" in hint


def test_parse_notes_primary():
    att = parse_context_attachment(
        {
            "primary": "notes",
            "notes": [{"path": "n.md", "title": "N"}],
            "files": [],
        }
    )
    assert att.primary == "notes"
    assert len(att.notes) == 1


def test_ingest_and_delete_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("chromadb")
    from agent_assistant import config as config_mod

    monkeypatch.setattr(config_mod.settings, "data_dir", tmp_path)
    (tmp_path / "knowledge" / "files").mkdir(parents=True)

    svc = KnowledgeService()
    svc.initialize()

    f1 = tmp_path / "doc1.md"
    f1.write_text("# Alpha\n\nUniqueTokenAlphaXYZ\n" * 20, encoding="utf-8")
    f2 = tmp_path / "doc2.md"
    f2.write_text("# Beta\n\nUniqueTokenBetaXYZ\n" * 20, encoding="utf-8")

    r1 = svc.ingest_path(f1, title="AlphaDoc")
    r2 = svc.ingest_path(f2, title="BetaDoc")
    assert r1["ok"] and r2["ok"]
    id1 = r1["file"]["file_id"]
    id2 = r2["file"]["file_id"]
    assert svc.store.count() >= 2

    deleted = svc.delete_file(id1)
    assert deleted["ok"]
    assert svc.registry.get(id1) is None
    assert svc.registry.get(id2) is not None
    # Remaining chunks should still be from file 2
    remaining = svc.store.count()
    assert remaining >= 1
    assert deleted["chunks_deleted"] >= 1
