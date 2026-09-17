"""Practice service end-to-end: generate → submit → review → report.

Uses a fake LLM (monkeypatched module attribute — quizgen/grader import it
lazily at call time) and a fresh PracticeStore pointed at tmp_path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import agent_assistant.llm.client as llm_module
import agent_assistant.practice.service as service_module
from agent_assistant.config import settings
from agent_assistant.practice.service import PracticeService
from agent_assistant.practice.store import PracticeStore

# ─── Fake LLM ─────────────────────────────────────────────────────


@dataclass
class FakeMessage:
    content: str = ""


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice] = field(default_factory=list)


class FakeLLM:
    """Scripted chat() — pops the next canned response per call."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages=None, temperature=0.7, response_format=None, **kwargs):
        self.calls.append(
            {"messages": messages, "response_format": response_format}
        )
        raw = self.responses.pop(0) if self.responses else "{}"
        return FakeResponse(choices=[FakeChoice(message=FakeMessage(content=raw))])


VALID_QUIZ = json.dumps(
    {
        "title": "练习：笔记A",
        "questions": [
            {
                "type": "choice",
                "question": "Python 是什么类型的语言？",
                "options": ["A. 编译型", "B. 解释型", "C. 汇编", "D. 机器码"],
                "answer": "1",
                "explain": "材料原句：Python 是解释型语言",
                "difficulty": "basic",
            },
            {
                "type": "choice",
                "question": "以下哪个是 Python 关键字？",
                "options": ["A. func", "B. define", "C. def", "D. lambda2"],
                "answer": "2",
                "explain": "def 是关键字",
                "difficulty": "basic",
            },
            {
                "type": "short",
                "question": "简述列表与元组的区别。",
                "answer": "列表可变，元组不可变",
                "explain": "材料：list mutable, tuple immutable",
                "difficulty": "understand",
            },
        ],
    },
    ensure_ascii=False,
)

GRADE_BATCH = json.dumps(
    {
        "results": [
            {"id": "__QID__", "score": 0.5, "feedback": "漏了不可变这一点"}
        ]
    },
    ensure_ascii=False,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp notes dir + fresh store + injectable fake LLM."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = PracticeStore(tmp_path / "practice.db")
    svc = PracticeService()
    # Service reads practice_store as a module-level name at call time.
    monkeypatch.setattr(service_module, "practice_store", store)
    fake_holder: dict[str, FakeLLM] = {}

    def set_fake(responses: list[str]) -> FakeLLM:
        fake = FakeLLM(responses)
        fake_holder["fake"] = fake
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        return fake

    # note on disk
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "python.md").write_text(
        "---\ntitle: \"Python 基础\"\n---\n\nPython 是解释型语言。"
        "list mutable, tuple immutable。def 是关键字。",
        encoding="utf-8",
    )

    yield SimpleNamespace(
        svc=svc, store=store, set_fake=set_fake, tmp_path=tmp_path
    )
    store.close()


# ─── Generate ─────────────────────────────────────────────────────


class TestGenerate:
    def test_generate_note_happy_path(self, env):
        fake = env.set_fake([VALID_QUIZ])
        res = env.svc.generate("note", "python.md", 3, "mixed")
        assert res["ok"] is True
        assert res["title"] == "练习：笔记A"
        assert res["source_title"] == "Python 基础"
        assert len(res["questions"]) == 3
        assert all(q["id"] for q in res["questions"])
        # JSON mode requested
        assert fake.calls[0]["response_format"] == {"type": "json_object"}
        # persisted
        sess = env.store.get_session(res["session_id"])
        assert sess["question_count"] == 3
        assert len(env.store.get_session_questions(res["session_id"])) == 3

    def test_generate_kb_reads_stored_copy(self, env, monkeypatch):
        stored = env.tmp_path / "kb_copy.txt"
        stored.write_text("知识库全文内容，足够长。", encoding="utf-8")
        entry = SimpleNamespace(title="KB 文档", stored_path=str(stored))

        class FakeRegistry:
            def get(self, file_id):
                return entry if file_id == "file1" else None

        from agent_assistant.knowledge.service import knowledge_service

        monkeypatch.setattr(knowledge_service, "_initialized", True)
        monkeypatch.setattr(knowledge_service, "_registry", FakeRegistry())

        captured = {}

        def fake_gen(material, count, qtype, *, min_questions=1):
            captured["material"] = material
            raise service_module.QuizGenError("stop")

        monkeypatch.setattr(service_module, "generate_quiz", fake_gen)
        env.svc.generate("kb", "file1", 3, "mixed")
        assert captured["material"] == "知识库全文内容，足够长。"

    def test_generate_kb_falls_back_to_chunks(self, env, monkeypatch):
        entry = SimpleNamespace(title="KB 文档", stored_path=str(env.tmp_path / "gone.txt"))

        class FakeRegistry:
            def get(self, file_id):
                return entry

        class FakeKNService:
            registry = FakeRegistry()

            def retrieve(self, query, n_results=5):
                return [
                    {"content": "chunk1", "file_id": "file1"},
                    {"content": "chunk2", "metadata": {"file_id": "other"}},
                    {"content": "chunk3", "file_id": "file1"},
                ]

        import agent_assistant.knowledge.service as ks_module

        monkeypatch.setattr(ks_module, "knowledge_service", FakeKNService())

        captured = {}

        def fake_gen(material, count, qtype, *, min_questions=1):
            captured["material"] = material
            raise service_module.QuizGenError("stop")

        monkeypatch.setattr(service_module, "generate_quiz", fake_gen)
        env.svc.generate("kb", "file1", 3, "mixed")
        assert captured["material"] == "chunk1\n\nchunk3"

    def test_generate_retries_with_problems(self, env):
        bad = json.dumps(
            {"title": "T", "questions": [
                {"type": "choice", "question": "?", "options": ["A"], "answer": "9"}
            ]}
        )
        good = json.dumps(
            {"title": "T", "questions": [
                {"type": "short", "question": "简答题", "answer": "答案", "explain": "e"}
            ]}
        )
        fake = env.set_fake([bad, good])
        res = env.svc.generate("note", "python.md", 1, "mixed")
        assert res["ok"] is True
        assert len(res["questions"]) == 1
        assert len(fake.calls) == 2
        # retry prompt carries the problems
        retry_user = fake.calls[1]["messages"][1]["content"]
        assert "重新输出" in retry_user

    def test_generate_still_bad_errors(self, env):
        bad = (
            '{"title": "T", "questions": '
            '[{"type": "choice", "question": "?", "options": ["A"], "answer": "9"}]}'
        )
        env.set_fake([bad, bad])
        res = env.svc.generate("note", "python.md", 1, "mixed")
        assert res["ok"] is False
        assert "有效题目" in res["error"]

    def test_generate_unknown_source(self, env):
        env.set_fake([VALID_QUIZ])
        res = env.svc.generate("video", "x.mp4", 3, "mixed")
        assert res["ok"] is False

    def test_generate_missing_note(self, env):
        env.set_fake([VALID_QUIZ])
        res = env.svc.generate("note", "nope.md", 3, "mixed")
        assert res["ok"] is False

    def test_generate_traversal_rejected(self, env):
        env.set_fake([VALID_QUIZ])
        res = env.svc.generate("note", "../secret.md", 3, "mixed")
        assert res["ok"] is False


# ─── Submit ───────────────────────────────────────────────────────


def _make_session(env, *, n_choice=2, n_short=1):
    """Generate a session via service (fake quiz), return (session_id, questions)."""
    qs = []
    for i in range(n_choice):
        qs.append(
            {
                "type": "choice",
                "question": f"选择{i}",
                "options": ["A. a", "B. b", "C. c", "D. d"],
                "answer": "1",
                "explain": "e",
                "difficulty": "basic",
            }
        )
    for i in range(n_short):
        qs.append(
            {
                "type": "short",
                "question": f"简答{i}",
                "answer": "要点",
                "explain": "e",
                "difficulty": "understand",
            }
        )
    quiz_json = json.dumps({"title": "练习：笔记A", "questions": qs}, ensure_ascii=False)
    env.set_fake([quiz_json])
    res = env.svc.generate("note", "python.md", n_choice + n_short, "mixed")
    assert res["ok"] is True
    return res["session_id"], res["questions"]


class TestSubmit:
    def test_submit_mixed_local_choice_llm_short(self, env):
        session_id, questions = _make_session(env)
        choice_qs = [q for q in questions if q["type"] == "choice"]
        short_qs = [q for q in questions if q["type"] == "short"]

        # Batch grade response must reference the real short question id.
        grade = GRADE_BATCH.replace("__QID__", short_qs[0]["id"])
        fake = env.set_fake([grade])

        answers = [
            {"question_id": choice_qs[0]["id"], "user_answer": "1"},  # right
            {"question_id": choice_qs[1]["id"], "user_answer": "0"},  # wrong
            {"question_id": short_qs[0]["id"], "user_answer": "列表可以变"},
        ]
        res = env.svc.submit(session_id, answers)
        assert res["ok"] is True
        assert res["total"] == 3
        # short scored 0.5 < 0.6 threshold → only choice0 counts as correct
        assert res["correct_count"] == 1
        by_id = {p["question_id"]: p for p in res["per_question"]}
        assert by_id[choice_qs[0]["id"]]["is_correct"] is True
        assert by_id[choice_qs[1]["id"]]["is_correct"] is False
        assert by_id[short_qs[0]["id"]]["score"] == 0.5
        assert by_id[short_qs[0]["id"]]["verdict"] == "partial"
        # one batch LLM call with the short question id
        assert len(fake.calls) == 1
        assert short_qs[0]["id"] in fake.calls[0]["messages"][1]["content"]
        # session finalized
        sess = env.store.get_session(session_id)
        assert sess["submitted"] == 1
        assert sess["score_pct"] == round((1.0 + 0.0 + 0.5) / 3 * 100)
        # cards created
        assert env.store.due_info()["boxes"]  # cards exist

    def test_submit_choice_no_llm_call(self, env):
        session_id, questions = _make_session(env, n_choice=2, n_short=0)
        fake = env.set_fake([])  # any LLM call would pop empty → {} but we assert none
        answers = [
            {"question_id": q["id"], "user_answer": "1"} for q in questions
        ]
        res = env.svc.submit(session_id, answers)
        assert res["ok"] is True
        assert res["score_pct"] == 100
        assert fake.calls == []

    def test_submit_card_mode_passes_scores_through(self, env):
        session_id, questions = _make_session(env)
        fake = env.set_fake([])
        answers = [
            {
                "question_id": q["id"],
                "user_answer": "x",
                "score": 0.9 if q["type"] == "short" else 1.0,
                "feedback": "好",
            }
            for q in questions
        ]
        res = env.svc.submit(session_id, answers)
        assert res["ok"] is True
        assert res["correct_count"] == 3
        assert fake.calls == []
        by_id = {p["question_id"]: p for p in res["per_question"]}
        short = [q for q in questions if q["type"] == "short"][0]
        assert by_id[short["id"]]["feedback"] == "好"

    def test_submit_twice_rejected(self, env):
        session_id, questions = _make_session(env, n_choice=1, n_short=0)
        env.set_fake([])
        answers = [{"question_id": q["id"], "user_answer": "1"} for q in questions]
        assert env.svc.submit(session_id, answers)["ok"] is True
        res = env.svc.submit(session_id, answers)
        assert res["ok"] is False
        assert "已提交" in res["error"]

    def test_submit_unknown_session(self, env):
        assert env.svc.submit("nope", [])["ok"] is False

    def test_submit_updates_leitner_cards(self, env):
        session_id, questions = _make_session(env, n_choice=2, n_short=0)
        env.set_fake([])
        answers = [
            {"question_id": questions[0]["id"], "user_answer": "1"},
            {"question_id": questions[1]["id"], "user_answer": "0"},
        ]
        env.svc.submit(session_id, answers)
        conn = env.store._ensure_db()
        boxes = {
            r["question_id"]: r["box"]
            for r in conn.execute("SELECT question_id, box FROM cards").fetchall()
        }
        assert boxes[questions[0]["id"]] == 2  # correct → promoted
        assert boxes[questions[1]["id"]] == 1  # wrong → box 1


# ─── Review / wrong / history ─────────────────────────────────────


class TestReviewAndWrong:
    def test_start_review_uses_due_cards(self, env):
        session_id, questions = _make_session(env, n_choice=2, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id,
            [
                {"question_id": questions[0]["id"], "user_answer": "1"},
                {"question_id": questions[1]["id"], "user_answer": "0"},
            ],
        )
        # Force both due.
        conn = env.store._ensure_db()
        conn.execute("UPDATE cards SET due_at = 0")
        conn.commit()
        res = env.svc.start_review()
        assert res["ok"] is True
        assert res["mode"] == "card"
        assert len(res["questions"]) == 2
        # Reused question rows (same ids).
        assert {q["id"] for q in res["questions"]} == {q["id"] for q in questions}

    def test_start_review_empty(self, env):
        assert env.svc.start_review()["ok"] is False

    def test_start_wrong(self, env):
        session_id, questions = _make_session(env, n_choice=2, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id,
            [
                {"question_id": questions[0]["id"], "user_answer": "0"},  # wrong
                {"question_id": questions[1]["id"], "user_answer": "1"},  # right
            ],
        )
        res = env.svc.start_wrong(session_id)
        assert res["ok"] is True
        assert len(res["questions"]) == 1
        assert res["questions"][0]["id"] == questions[0]["id"]

    def test_start_wrong_no_wrong(self, env):
        session_id, questions = _make_session(env, n_choice=1, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id, [{"question_id": questions[0]["id"], "user_answer": "1"}]
        )
        res = env.svc.start_wrong(session_id)
        assert res["ok"] is False

    def test_history(self, env):
        assert env.svc.history()["sessions"] == []
        session_id, questions = _make_session(env, n_choice=1, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id, [{"question_id": questions[0]["id"], "user_answer": "1"}]
        )
        sessions = env.svc.history()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["score_pct"] == 100


# ─── session_detail (history replay) ──────────────────────────────


class TestSessionDetail:
    def test_detail_after_submit(self, env):
        session_id, questions = _make_session(env, n_choice=1, n_short=1)
        short_q = [q for q in questions if q["type"] == "short"][0]
        grade = GRADE_BATCH.replace("__QID__", short_q["id"])
        env.set_fake([grade])
        env.svc.submit(
            session_id,
            [
                {"question_id": questions[0]["id"], "user_answer": "1"},
                {"question_id": short_q["id"], "user_answer": "列表可以变"},
            ],
        )
        res = env.svc.session_detail(session_id)
        assert res["ok"] is True
        assert res["session"]["session_id"] == session_id
        assert res["session"]["submitted"] == 1
        assert len(res["questions"]) == 2
        assert [q["id"] for q in res["questions"]] == [
            q["id"] for q in questions
        ]
        attempts = {a["question_id"]: a for a in res["attempts"]}
        assert attempts[questions[0]["id"]]["user_answer"] == "1"
        assert attempts[questions[0]["id"]]["score"] == 1.0
        assert attempts[short_q["id"]]["score"] == 0.5
        assert "不可变" in attempts[short_q["id"]]["feedback"]

    def test_detail_unsubmitted_rejected(self, env):
        session_id, _ = _make_session(env, n_choice=1, n_short=0)
        res = env.svc.session_detail(session_id)
        assert res["ok"] is False
        assert "尚未提交" in res["error"]

    def test_detail_missing_session(self, env):
        assert env.svc.session_detail("nope")["ok"] is False


# ─── custom topic quiz (画像 + 一句话主题) ────────────────────────


class TestCustomGenerate:
    def test_generate_custom_with_profile(self, env, monkeypatch):
        quiz_json = json.dumps(
            {
                "title": "TCP 专题",
                "questions": [
                    {
                        "type": "short",
                        "category": "计算机网络",
                        "question": "为什么需要三次握手？",
                        "answer": "同步双方初始序列号",
                        "explain": "考点：握手目的",
                        "difficulty": "understand",
                    }
                ],
            },
            ensure_ascii=False,
        )
        fake = env.set_fake([quiz_json])

        class FakeUISettings:
            def get_setting(self, key, default=""):
                return {
                    "profile_major": "计算机科学与技术",
                    "profile_grade": "大三",
                    "profile_goal": "求职后端开发",
                    "profile_note": "",
                }.get(key, default)

        import agent_assistant.ui.store as ui_store_mod

        monkeypatch.setattr(ui_store_mod, "ui_store", FakeUISettings())

        res = env.svc.generate("custom", "TCP 三次握手", 3, "short")
        assert res["ok"] is True
        assert res["source_kind"] == "custom"
        assert res["questions"][0]["category"] == "计算机网络"
        # prompt carries profile + topic + engineering style
        user = fake.calls[0]["messages"][1]["content"]
        assert "专业：计算机科学与技术" in user
        assert "出题主题：TCP 三次握手" in user
        assert "偏工程化" in user
        # persisted with category
        qs = env.store.get_session_questions(res["session_id"])
        assert qs[0]["category"] == "计算机网络"

    def test_generate_custom_empty_topic(self, env):
        env.set_fake([])
        assert env.svc.generate("custom", "  ", 3, "mixed")["ok"] is False

    def test_count_clamped_to_10(self, env, monkeypatch):
        captured = {}

        def fake_gen(profile, topic, count, qtype, *, min_questions=1):
            captured["count"] = count
            raise service_module.QuizGenError("stop")

        monkeypatch.setattr(service_module, "generate_topic_quiz", fake_gen)
        env.svc.generate("custom", "主题", 99, "mixed")
        assert captured["count"] == 10


# ─── file source (精读联动「就这篇，考我」） ───────────────────────


class TestFileGenerate:
    def test_generate_from_local_file(self, env, tmp_path):
        f = tmp_path / "paper.md"
        f.write_text(
            "---\ntitle: \"测试材料\"\n---\n\nGraphRAG 结合图结构与向量检索。",
            encoding="utf-8",
        )
        env.set_fake([VALID_QUIZ])
        res = env.svc.generate("file", str(f), 3, "mixed")
        assert res["ok"] is True
        assert res["source_kind"] == "file"
        assert res["source_title"] == "paper"
        # session persisted with the path so replay/wrong-retry work
        sess = env.store.get_session(res["session_id"])
        assert sess["source_id"] == str(f)

    def test_generate_file_outside_jail_rejected(self, env, tmp_path, monkeypatch):
        f = tmp_path / "outside.md"
        f.write_text("内容", encoding="utf-8")
        env.set_fake([])

        import agent_assistant.tools.permission as perm

        monkeypatch.setattr(perm, "path_in_allowed_roots", lambda p: False)
        res = env.svc.generate("file", str(f), 3, "mixed")
        assert res["ok"] is False
        assert "工作区之外" in res["error"]

    def test_generate_file_missing_rejected(self, env, tmp_path):
        env.set_fake([])
        res = env.svc.generate("file", str(tmp_path / "gone.md"), 3, "mixed")
        assert res["ok"] is False

    def test_generate_file_directory_rejected(self, env, tmp_path):
        env.set_fake([])
        res = env.svc.generate("file", str(tmp_path), 3, "mixed")
        assert res["ok"] is False

    def test_generate_file_unsupported_binary(self, env, tmp_path):
        f = tmp_path / "blob.bin"
        f.write_bytes(bytes(range(256)) * 32)  # neither utf-8 nor gbk → binary
        env.set_fake([])
        res = env.svc.generate("file", str(f), 3, "mixed")
        assert res["ok"] is False


# ─── grade_short / save_report ────────────────────────────────────


class TestGradeShortAndReport:
    def test_grade_short(self, env):
        session_id, questions = _make_session(env, n_choice=0, n_short=1)
        short = questions[0]
        fake = env.set_fake(['{"score": 0.9, "feedback": "很好"}'])
        res = env.svc.grade_short(short["id"], "我的回答")
        assert res["ok"] is True
        assert res["score"] == 0.9
        assert res["verdict"] == "right"
        assert fake.calls[0]["response_format"] == {"type": "json_object"}

    def test_grade_short_missing_question(self, env):
        env.set_fake([])
        assert env.svc.grade_short("nope", "a")["ok"] is False

    def test_save_report_with_wrong(self, env, monkeypatch):
        session_id, questions = _make_session(env, n_choice=2, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id,
            [
                {"question_id": questions[0]["id"], "user_answer": "0"},
                {"question_id": questions[1]["id"], "user_answer": "1"},
            ],
        )
        saved = {}

        class FakeResult:
            ok = True
            data = {"filename": "report.md"}
            error = None

        class FakeTool:
            def execute(self, title, content, tag=""):
                saved.update({"title": title, "content": content, "tag": tag})
                return FakeResult()

        import agent_assistant.tools.save_note as sn_module

        monkeypatch.setattr(sn_module, "SaveNoteTool", FakeTool)
        res = env.svc.save_report(session_id)
        assert res["ok"] is True
        assert saved["tag"] == "练习报告"
        assert "练习报告" in saved["title"]
        # wrong question appears with user answer + reference
        assert questions[0]["question"] in saved["content"]
        assert "你的答案" in saved["content"]

    def test_save_report_all_correct(self, env, monkeypatch):
        session_id, questions = _make_session(env, n_choice=1, n_short=0)
        env.set_fake([])
        env.svc.submit(
            session_id, [{"question_id": questions[0]["id"], "user_answer": "1"}]
        )
        saved = {}

        class FakeResult:
            ok = True
            data = {"filename": "report.md"}
            error = None

        class FakeTool:
            def execute(self, title, content, tag=""):
                saved["content"] = content
                return FakeResult()

        import agent_assistant.tools.save_note as sn_module

        monkeypatch.setattr(sn_module, "SaveNoteTool", FakeTool)
        env.svc.save_report(session_id)
        assert "没有错题" in saved["content"]
