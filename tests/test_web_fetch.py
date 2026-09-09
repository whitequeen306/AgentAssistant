"""Tests for read_page fetch/extract helpers (empty HTML must not hit trafilatura)."""

from __future__ import annotations

import types

import pytest

from agent_assistant.tools import web_tools as wt
from agent_assistant.tools.web_tools import ReadPageTool, extract_main_text


def test_extract_main_text_empty_returns_empty_without_crash():
    text, method = extract_main_text("")
    assert text == ""
    assert method == "empty"


def test_extract_main_text_whitespace_only():
    text, method = extract_main_text("   \n\t  ")
    assert text == ""
    assert method == "empty"


def test_extract_main_text_basic_fallback():
    html = "<html><body><p>Hello research world content here enough chars.</p></body></html>"
    text, method = extract_main_text(html)
    assert "Hello research" in text
    assert method in ("trafilatura", "basic")


def test_fetch_html_raises_on_empty_body(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def geturl(self):
            return "https://example.com/empty"

        def read(self):
            return b""

        @property
        def headers(self):
            return {}

    monkeypatch.setitem(
        __import__("sys").modules,
        "trafilatura",
        types.SimpleNamespace(fetch_url=lambda _u: None, extract=lambda *_a, **_k: None),
    )
    monkeypatch.setattr(wt.urllib.request, "urlopen", lambda *_a, **_k: _Resp())

    with pytest.raises(ValueError, match="empty HTML"):
        wt.fetch_html("https://example.com/empty")


def test_read_page_empty_html_returns_failure(monkeypatch):
    monkeypatch.setattr(
        wt,
        "fetch_html",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("page returned empty HTML")),
    )
    result = ReadPageTool().execute(url="https://example.com/x")
    assert not result.ok
    assert "empty" in (result.error or "").lower()
