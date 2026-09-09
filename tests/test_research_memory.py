"""L1/L2 research run artifacts + local source validation."""

from __future__ import annotations

from pathlib import Path

from agent_assistant.research.local_sources import (
    LocalSource,
    clear_pending_local_sources,
    load_source_text,
    resolve_source_path,
    set_pending_local_sources,
    take_pending_local_sources,
)
from agent_assistant.research.run_store import (
    ResearchRun,
    extract_refs_from_tool_result,
    summarize_tool_result,
)


def test_l1_append_and_l2_consolidate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_assistant.research.run_store.settings.data_dir", tmp_path
    )
    run = ResearchRun(goal="test goal")
    run.log_l1(
        tool="web_search",
        args={"query": "agents"},
        summary="Paper A | Paper B",
        source_refs=["https://example.com/a", "https://example.com/b"],
        turn=1,
    )
    run.log_l1(
        tool="read_page",
        args={"url": "https://example.com/a"},
        summary="Full article about agents…",
        source_refs=["https://example.com/a"],
        turn=2,
    )
    events = run.read_l1()
    assert len(events) == 2
    assert events[0]["id"] == "E1"
    assert run.trace_path.is_file()

    findings = run.consolidate_l2_from_l1()
    assert len(findings) == 2  # unique URLs
    assert run.findings_path.is_file()
    text = run.findings_path.read_text(encoding="utf-8")
    assert "https://example.com/a" in text
    assert "L2" in text or "Findings" in text

    run.write_report("# Report\nDone.")
    assert run.report_path.read_text(encoding="utf-8").startswith("# Report")


def test_extract_refs_from_web_search_result():
    refs = extract_refs_from_tool_result(
        "web_search",
        {
            "ok": True,
            "data": {
                "results": [
                    {"title": "A", "url": "https://a.example"},
                    {"title": "B", "url": "https://b.example"},
                ]
            },
        },
    )
    assert "https://a.example" in refs
    assert "https://b.example" in refs


def test_summarize_tool_result_failure():
    s = summarize_tool_result("web_search", {"ok": False, "error": "down"})
    assert "FAIL" in s


def test_pending_local_sources_roundtrip():
    clear_pending_local_sources()
    set_pending_local_sources(
        [
            {"kind": "note", "path": "a.md", "title": "A"},
            {"kind": "file", "path": "C:/tmp/x.txt"},
        ]
    )
    got = take_pending_local_sources()
    assert len(got) == 2
    assert got[0].kind == "note"
    assert take_pending_local_sources() == []


def test_resolve_note_and_file(tmp_path, monkeypatch):
    notes = tmp_path / "notes"
    notes.mkdir()
    note = notes / "hello.md"
    note.write_text("note body", encoding="utf-8")
    monkeypatch.setattr(
        "agent_assistant.research.local_sources.settings.data_dir", tmp_path
    )
    monkeypatch.setattr(
        "agent_assistant.research.local_sources.settings.notes_dir", notes
    )

    ok_path = resolve_source_path(LocalSource(kind="note", path="hello.md"))
    assert ok_path == note.resolve()

    bad = resolve_source_path(LocalSource(kind="note", path="../etc/passwd"))
    assert bad is None

    f = tmp_path / "doc.txt"
    f.write_text("local file", encoding="utf-8")
    ok_f = resolve_source_path(LocalSource(kind="file", path=str(f)))
    assert ok_f == f.resolve()
    ok, text = load_source_text(LocalSource(kind="file", path=str(f)))
    assert ok and "local file" in text

    exe = tmp_path / "x.exe"
    exe.write_bytes(b"MZ")
    assert resolve_source_path(LocalSource(kind="file", path=str(exe))) is None
