"""「写了但没通电」的回归保护。

背景：项目里出过一类隐蔽问题——代码写完整、测试也过了，但**没有任何调用方**，
于是它安静地不工作。最严重的一次是三级搜索源用 `os.environ.get` 读 key，而
pydantic-settings 根本不写 os.environ，导致它们从写出那天起就没发过一次请求，
级联静默落到返回旅游攻略的 Bing，工具还报 `ok: true`。

这类问题单元测试抓不到（mock 掩盖集成层），静态分析也不报（它是"合法"代码）。

本文件只做**精准断言**：锁住已修的三个点和已知的失效模式，不做全量孤儿扫描
（那是 noise 很大的一次性排查手段，不适合当门禁）。
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "agent_assistant"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


class TestSearchBackend:
    """搜索层曾整体失效且伪装成功，这里锁住修复。"""

    def test_only_one_search_provider(self):
        """单源：多级级联曾导致失败被静默接管，返回无关结果。"""
        web = _read("tools/web_tools.py")
        providers = [
            line
            for line in web.splitlines()
            if line.startswith("def _search_")
        ]
        assert len(providers) == 1, f"搜索源应只剩 AnySearch，实际：{providers}"

    def test_credentials_come_from_dotenv_not_os_environ(self):
        """os.environ 读不到 pydantic-settings 的 .env —— 这是当年事故的根因。"""
        web = _read("tools/web_tools.py")
        assert "env_or_dotenv(" in web
        assert 'os.environ.get("ANYSEARCH_API_KEY"' not in web

    def test_failure_is_explicit_not_silent_fallback(self):
        """失败必须报错，不能静默换源后继续返回 ok。"""
        web = _read("tools/web_tools.py")
        for category in ("missing_credential", "search_unavailable", "search_irrelevant"):
            assert category in web, f"缺少显式失败类型：{category}"

    def test_relevance_gate_exists(self):
        """相关度闸门：查不到相关内容要判失败，而不是把垃圾当结果喂给模型。"""
        web = _read("tools/web_tools.py")
        assert "_RELEVANCE_MIN_SCORE" in web
        assert "_gate_results" in web


class TestGoalTrackWiring:
    """目标轨道（goals）曾有一批数据就绪但零消费者的功能。"""

    def test_due_milestones_has_a_consumer(self):
        """节点提醒：数据层算出来了，却没人调——用户打开「规划」之外永远看不到。"""
        import inspect

        from agent_assistant.ui.bridge import ApiBridge

        assert hasattr(ApiBridge, "due_milestone_reminders")
        assert "due_milestones(" in _read("ui/bridge.py")
        # get_init_data 必须把到期节点带给前端，否则提醒永远不出现
        assert "due_milestones" in inspect.getsource(ApiBridge.get_init_data)

    def test_chip_hints_reach_the_prompt(self):
        """chip_hints 写好了却从没进过开场建议的提示词。"""
        from agent_assistant.ui.chips import build_chips_messages

        user = build_chips_messages(
            {
                "grade": "大三",
                "track": "考研 · 2027 考研",
                "track_hints": ["对比我的目标院校近三年复试线"],
            },
            [],
            [],
        )[1]["content"]
        assert "目标轨道" in user
        assert "近三年复试线" in user

    def test_research_template_carries_cycle_year(self):
        """无年份信息曾导致报告整份滞后一代（2026 招生季 vs 2027 级）。"""
        from agent_assistant.goals.registry import active_cycle_year, get_spec

        text = get_spec("postgrad").render_research_template(
            cycle_year=active_cycle_year()
        )
        assert "{cycle_year}" not in text
        assert "年度口径" in text
