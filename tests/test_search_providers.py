"""web_search: AnySearch provider + 相关度闸门。

历史背景：2026-09-11 一次择校调研翻车——BoCha 额度耗尽后静默降级到 Bing，
Bing 对多词查询返回通用热门内容（考研查询返回「哈尔滨旅游攻略」），却仍报
成功，子 Agent 于是拿旅游页面当招生资料反复「交叉核验」。现在只保留一个源，
且任何降级都必须显式失败。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from agent_assistant.tools import web_tools
from agent_assistant.tools.web_tools import (
    _RELEVANCE_MIN_SCORE,
    WebSearchTool,
    _gate_results,
    _query_terms,
    _relevance_score,
    _search_anysearch,
)


class _FakeResponse:
    def __init__(self, payload: dict | bytes) -> None:
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        )

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _patch_urlopen(monkeypatch, *, payload=None, exc: Exception | None = None):
    def fake_urlopen(*_a, **_k):
        if exc is not None:
            raise exc
        return _FakeResponse(payload)

    monkeypatch.setattr(web_tools.urllib.request, "urlopen", fake_urlopen)


def _ok_payload(*results) -> dict:
    return {
        "code": 0,
        "message": "success",
        "request_id": "rid",
        "data": {"results": list(results)},
    }


def _item(title: str, url: str, snippet: str = "") -> dict:
    return {"title": title, "url": url, "snippet": snippet, "content": snippet}


# ─── AnySearch provider ─────────────────────────────────────────────


class TestAnySearchProvider:
    def test_parses_results(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            payload=_ok_payload(
                _item(
                    "哈工大深圳2025年硕士研究生招生学科目录",
                    "https://yzb.hitsz.edu.cn/x.pdf",
                    "408 计算机学科专业基础",
                ),
                _item("招生章程", "https://yzb.hit.edu.cn/8823/list.htm", "按需招生"),
            ),
        )
        results = _search_anysearch("哈尔滨工业大学 招生专业目录", "key")
        assert len(results) == 2
        assert results[0]["url"] == "https://yzb.hitsz.edu.cn/x.pdf"
        assert "408" in results[0]["snippet"]

    def test_content_used_when_snippet_missing(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            payload=_ok_payload(
                {"title": "t", "url": "https://a.com", "content": "正文"}
            ),
        )
        assert _search_anysearch("q", "key")[0]["snippet"] == "正文"

    def test_items_without_url_are_skipped(self, monkeypatch):
        _patch_urlopen(
            monkeypatch,
            payload=_ok_payload(
                {"title": "no url", "snippet": "x"},
                _item("t", "https://a.com"),
            ),
        )
        assert len(_search_anysearch("q", "key")) == 1

    def test_non_zero_code_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, payload={"code": 401, "message": "invalid key"})
        assert _search_anysearch("q", "bad") is None

    def test_empty_results_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, payload=_ok_payload())
        assert _search_anysearch("q", "key") is None

    def test_http_error_returns_none(self, monkeypatch):
        err = urllib.error.HTTPError(
            "https://api.anysearch.com/v1/search",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"code":"403","message":"no quota"}'),
        )
        _patch_urlopen(monkeypatch, exc=err)
        assert _search_anysearch("q", "key") is None

    def test_network_error_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, exc=OSError("connection refused"))
        assert _search_anysearch("q", "key") is None


# ─── 相关度闸门 ──────────────────────────────────────────────────────


class TestQueryTerms:
    def test_splits_cjk_runs_and_ascii_words(self):
        assert _query_terms("哈尔滨工业大学 研究生招生专业目录 CS408") == [
            "哈尔滨工业大学",
            "研究生招生专业目录",
            "cs408",
        ]

    def test_stopword_only_terms_dropped(self):
        assert _query_terms("的和与") == []

    def test_terms_capped(self):
        assert len(_query_terms(" ".join(f"词{i}" for i in range(30)))) <= 12


class TestRelevanceScore:
    def test_matching_text_scores_above_threshold(self):
        score = _relevance_score(
            "哈尔滨工业大学 招生专业目录",
            "哈尔滨工业大学（深圳）2025年硕士研究生招生学科目录",
        )
        assert score >= _RELEVANCE_MIN_SCORE

    def test_tourism_spam_scores_below_threshold(self):
        """这就是那次翻车时 Bing 返回的东西。"""
        score = _relevance_score(
            "哈尔滨工业大学 研究生招生专业目录 计算机",
            "哈尔滨市_百度百科 哈尔滨旅游攻略景点大全，必去十大景点推荐",
        )
        assert score < _RELEVANCE_MIN_SCORE

    def test_ascii_garbage_scores_below_threshold(self):
        score = _relevance_score(
            "Harbin Institute of Technology graduate admission",
            "Ice City Harbin: 13 Best Places to Visit",
        )
        assert score < _RELEVANCE_MIN_SCORE

    def test_empty_query_passes_everything(self):
        assert _relevance_score("", "任意内容") == 1.0


class TestGateResults:
    def test_drops_irrelevant_keeps_relevant(self):
        results = [
            {
                "title": "哈尔滨工业大学研究生招生网",
                "url": "https://yzb.hit.edu.cn",
                "snippet": "招生专业目录",
            },
            {
                "title": "哈尔滨旅游攻略",
                "url": "https://sohu.com/a/1",
                "snippet": "必去十大景点",
            },
        ]
        kept = _gate_results("哈尔滨工业大学 研究生招生专业目录", results)
        assert kept is not None
        assert len(kept) == 1
        assert kept[0]["url"] == "https://yzb.hit.edu.cn"

    def test_all_irrelevant_returns_none(self):
        results = [
            {
                "title": "哈尔滨市_百度百科",
                "url": "https://baike.baidu.com/x",
                "snippet": "哈尔滨市人民政府",
            },
            {"title": "百度地图", "url": "https://map.baidu.com/", "snippet": ""},
        ]
        assert _gate_results("哈尔滨工业大学 研究生招生专业目录", results) is None

    def test_empty_input_returns_none(self):
        assert _gate_results("q", []) is None


# ─── Tool 行为 ───────────────────────────────────────────────────────


class TestWebSearchTool:
    def test_missing_api_key_is_explicit_failure(self, monkeypatch):
        # 必须连「即时读 .env」的回退一起屏蔽 —— 开发机上 .env 里确实有 key，
        # 只 delenv 是挡不住的。
        monkeypatch.setattr(web_tools, "env_or_dotenv", lambda *_a, **_k: "")
        result = WebSearchTool().execute(query="q")
        assert result.ok is False
        assert "ANYSEARCH_API_KEY" in result.error

    def test_provider_failure_is_explicit(self, monkeypatch):
        monkeypatch.setenv("ANYSEARCH_API_KEY", "k")
        _patch_urlopen(monkeypatch, exc=OSError("boom"))
        result = WebSearchTool().execute(query="东北大学2026年硕士研究生招生专业目录")
        assert result.ok is False
        assert "unavailable" in result.error

    def test_gate_failure_is_explicit(self, monkeypatch):
        """源返回了东西但全不相关 —— 必须报错，不能当成功喂给模型。"""
        monkeypatch.setenv("ANYSEARCH_API_KEY", "k")
        _patch_urlopen(
            monkeypatch,
            payload=_ok_payload(
                _item(
                    "哈尔滨市_百度百科",
                    "https://baike.baidu.com/x",
                    "哈尔滨市人民政府",
                ),
                _item("百度地图", "https://map.baidu.com/", ""),
            ),
        )
        result = WebSearchTool().execute(query="哈尔滨工业大学 研究生招生专业目录")
        assert result.ok is False
        assert "irrelevant" in result.error

    def test_success_returns_normalised_shape(self, monkeypatch):
        monkeypatch.setenv("ANYSEARCH_API_KEY", "k")
        _patch_urlopen(
            monkeypatch,
            payload=_ok_payload(
                _item(
                    "哈尔滨工业大学（深圳）2025年硕士研究生招生学科目录",
                    "https://yzb.hitsz.edu.cn/x.pdf",
                    "①101 思想政治理论②201 英语一③301 数学一④408 计算机学科专业基础",
                )
            ),
        )
        result = WebSearchTool().execute(query="哈尔滨工业大学 硕士研究生招生专业目录")
        assert result.ok is True
        assert result.data["source"] == "anysearch"
        assert result.data["results"][0]["url"].endswith("x.pdf")

    def test_empty_query_rejected(self, monkeypatch):
        monkeypatch.setenv("ANYSEARCH_API_KEY", "k")
        assert WebSearchTool().execute(query="  ").ok is False

    def test_description_promises_no_silent_fallback(self):
        desc = WebSearchTool().description
        assert "AnySearch" in desc
        assert "does not" in desc and "silently" in desc
        # 旧的级联描述不能再出现
        assert "BoCha" not in desc and "Bing" not in desc


@pytest.mark.parametrize("query", ["", "   "])
def test_blank_query_fails_everywhere(query):
    assert WebSearchTool().execute(query=query).ok is False


class TestEnvResolution:
    """`.env` 定位不能依赖 CWD —— 从别处启动曾导致 key 「填了却读不到」。"""

    def test_candidates_include_project_root(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        monkeypatch.chdir(tmp_path)  # 模拟从别处启动
        cands = [str(p) for p in cfg.env_file_candidates()]
        assert any(p.endswith(".env") for p in cands)

    def test_env_or_dotenv_reads_file_without_restart(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        f = tmp_path / ".env"
        f.write_text("SOME_KEY=from_file\n# COMMENT=1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SOME_KEY", raising=False)
        assert cfg.env_or_dotenv("SOME_KEY") == "from_file"

    def test_process_env_wins_over_file(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        (tmp_path / ".env").write_text("SOME_KEY=from_file\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SOME_KEY", "from_env")
        assert cfg.env_or_dotenv("SOME_KEY") == "from_env"

    def test_missing_key_returns_default(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DEFINITELY_ABSENT", raising=False)
        assert cfg.env_or_dotenv("DEFINITELY_ABSENT", "dflt") == "dflt"


class TestEnvResolution:
    """`.env` 定位不能依赖 CWD —— 从别处启动曾导致 key 「填了却读不到」。"""

    def test_candidates_include_project_root(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        monkeypatch.chdir(tmp_path)  # 模拟从别处启动
        cands = [str(p) for p in cfg.env_file_candidates()]
        assert any(p.endswith(".env") for p in cands)

    def test_env_or_dotenv_reads_file_without_restart(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        f = tmp_path / ".env"
        f.write_text("SOME_KEY=from_file\n# COMMENT=1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SOME_KEY", raising=False)
        assert cfg.env_or_dotenv("SOME_KEY") == "from_file"

    def test_process_env_wins_over_file(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        (tmp_path / ".env").write_text("SOME_KEY=from_file\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SOME_KEY", "from_env")
        assert cfg.env_or_dotenv("SOME_KEY") == "from_env"

    def test_missing_key_returns_default(self, tmp_path, monkeypatch):
        from agent_assistant import config as cfg

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DEFINITELY_ABSENT", raising=False)
        assert cfg.env_or_dotenv("DEFINITELY_ABSENT", "dflt") == "dflt"
