"""Tests for the hosted search API providers (BoCha / Brave) and cascade order."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from agent_assistant.tools import web_tools
from agent_assistant.tools.web_tools import (
    WebSearchTool,
    _search_bocha,
    _search_brave,
)


def _fake_response(payload: dict) -> io.BytesIO:
    body = json.dumps(payload).encode("utf-8")
    resp = io.BytesIO(body)
    resp.__enter__ = lambda s: s  # type: ignore[attr-defined]
    resp.__exit__ = lambda *a: False  # type: ignore[attr-defined]
    return resp


class TestBocha:
    def test_parses_webpages(self):
        payload = {
            "code": 200,
            "data": {
                "webPages": {
                    "value": [
                        {"name": "东北大学2026硕士招生目录", "url": "http://yz.neu.edu.cn/a",
                         "summary": "含材料科学与工程学院招生专业"},
                        {"name": "无效项", "url": ""},
                        {"name": "大连理工研究生院", "url": "http://gs.dlut.edu.cn/b",
                         "snippet": "招生专业目录查询"},
                    ]
                }
            },
        }
        with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            results = _search_bocha("东北大学2026硕士招生目录", "key")
        assert results is not None
        assert len(results) == 2  # empty-url entry dropped
        assert results[0]["title"] == "东北大学2026硕士招生目录"
        assert results[0]["snippet"] == "含材料科学与工程学院招生专业"
        # falls back to snippet when summary missing
        assert results[1]["snippet"] == "招生专业目录查询"

    def test_non_200_code_returns_none(self):
        payload = {"code": 403, "msg": "quota exhausted"}
        with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            assert _search_bocha("q", "key") is None

    def test_network_error_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            assert _search_bocha("q", "key") is None


class TestBrave:
    def test_parses_web_results(self):
        payload = {
            "web": {
                "results": [
                    {"title": "R1", "url": "https://a.cn", "description": "d1"},
                    {"title": "R2", "url": "https://b.cn", "description": "d2"},
                ]
            }
        }
        with patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            results = _search_brave("test", "key")
        assert results == [
            {"title": "R1", "url": "https://a.cn", "snippet": "d1"},
            {"title": "R2", "url": "https://b.cn", "snippet": "d2"},
        ]

    def test_cjk_query_gets_zh_params(self):
        captured: dict = {}

        def _spy(req, timeout=0):
            captured["url"] = req.full_url
            return _fake_response({"web": {"results": []}})

        with patch("urllib.request.urlopen", side_effect=_spy):
            _search_brave("东北大学 招生目录", "key")
        assert "search_lang=zh-hans" in captured["url"]
        assert "country=CN" in captured["url"]

    def test_latin_query_gets_en_params(self):
        captured: dict = {}

        def _spy(req, timeout=0):
            captured["url"] = req.full_url
            return _fake_response({"web": {"results": []}})

        with patch("urllib.request.urlopen", side_effect=_spy):
            _search_brave("python asyncio", "key")
        assert "search_lang=en" in captured["url"]
        assert "zh-hans" not in captured["url"]

    def test_empty_results_returns_none(self):
        with patch("urllib.request.urlopen",
                   return_value=_fake_response({"web": {"results": []}})):
            assert _search_brave("q", "key") is None


class TestCascadeOrder:
    """Provider priority: bocha > brave > searxng > tavily > bing."""

    def _run(self, monkeypatch, env: dict[str, str], outcomes: dict[str, list | None]):
        for k in ("BOCHA_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(web_tools, "_search_bocha",
                            lambda q, k: outcomes.get("bocha"))
        monkeypatch.setattr(web_tools, "_search_brave",
                            lambda q, k: outcomes.get("brave"))
        monkeypatch.setattr(web_tools, "_search_searxng",
                            lambda q: outcomes.get("searxng"))
        monkeypatch.setattr(web_tools, "_search_tavily",
                            lambda q, k: outcomes.get("tavily"))
        monkeypatch.setattr(web_tools, "_search_bing",
                            lambda q: outcomes.get("bing"))
        return WebSearchTool().execute(query="q")

    def test_bocha_wins_when_key_set(self, monkeypatch):
        hit = [{"title": "t", "url": "https://u", "snippet": "s"}]
        res = self._run(monkeypatch, {"BOCHA_API_KEY": "k"}, {"bocha": hit})
        assert res.ok and res.data["source"] == "bocha"

    def test_brave_second(self, monkeypatch):
        hit = [{"title": "t", "url": "https://u", "snippet": "s"}]
        res = self._run(monkeypatch, {"BRAVE_API_KEY": "k"}, {"brave": hit})
        assert res.ok and res.data["source"] == "brave"

    def test_searxng_used_when_no_keys(self, monkeypatch):
        hit = [{"title": "t", "url": "https://u", "snippet": "s"}]
        res = self._run(monkeypatch, {}, {"searxng": hit})
        assert res.ok and res.data["source"] == "searxng"

    def test_bocha_failure_falls_through_to_brave(self, monkeypatch):
        hit = [{"title": "t", "url": "https://u", "snippet": "s"}]
        res = self._run(
            monkeypatch,
            {"BOCHA_API_KEY": "k", "BRAVE_API_KEY": "k"},
            {"bocha": None, "brave": hit},
        )
        assert res.ok and res.data["source"] == "brave"

    def test_bing_last_resort(self, monkeypatch):
        hit = [{"title": "t", "url": "https://u", "snippet": "s"}]
        res = self._run(monkeypatch, {"BOCHA_API_KEY": "k"}, {"bing": hit})
        assert res.ok and res.data["source"] == "bing"

    def test_all_fail_returns_failure(self, monkeypatch):
        res = self._run(monkeypatch, {"BOCHA_API_KEY": "k"}, {})
        assert not res.ok
