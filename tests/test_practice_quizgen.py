"""Quiz generation parsing/validation + grader parsing/clamping."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

import agent_assistant.llm.client as llm_module
import agent_assistant.practice.grader as grader_module
from agent_assistant.practice.grader import grade_batch, grade_one, verdict_of
from agent_assistant.practice.quizgen import (
    QuizGenError,
    _extract_json,
    _normalize_choice_answer,
    _parse_quiz,
    build_topic_quiz_messages,
    generate_quiz,
    generate_topic_quiz,
)

# ─── Choice answer normalization ──────────────────────────────────


class TestNormalizeChoiceAnswer:
    @pytest.mark.parametrize("raw,expected", [
        ("0", 0), ("3", 3),
        ("A", 0), ("B", 1), ("a", 0), ("d", 3),
        ("A.", 0), ("B、", 1),
    ])
    def test_valid(self, raw, expected):
        assert _normalize_choice_answer(raw) == expected

    @pytest.mark.parametrize("raw", ["4", "-1", "E", "", "1.5", "abc"])
    def test_invalid(self, raw):
        assert _normalize_choice_answer(raw) is None


# ─── JSON extraction / parse / salvage ────────────────────────────


class TestParseQuiz:
    def test_extract_json_strips_fences(self):
        raw = '```json\n{"a": 1}\n```'
        assert _extract_json(raw) == {"a": 1}

    def test_extract_json_prose_wrapped(self):
        assert _extract_json('好的，这是结果：{"a": 1} 请查收') == {"a": 1}

    def test_extract_json_invalid(self):
        assert _extract_json("no json here") is None
        assert _extract_json('{"broken": ') is None

    def test_parse_valid_quiz(self):
        quiz, problems = _parse_quiz(json.dumps({
            "title": "T",
            "questions": [
                {"type": "choice", "question": "Q1", "options": ["A", "B", "C", "D"],
                 "answer": "2", "explain": "e", "difficulty": "apply"},
                {"type": "short", "question": "Q2", "answer": "要点", "explain": "e"},
            ],
        }))
        assert problems == []
        assert quiz.title == "T"
        assert len(quiz.questions) == 2
        assert quiz.questions[0].answer == "2"
        assert quiz.questions[0].difficulty == "apply"
        assert quiz.questions[1].options is None

    def test_parse_salvages_valid_drops_invalid(self):
        good_choice = {
            "type": "choice",
            "question": "good",
            "options": ["A", "B", "C", "D"],
            "answer": "0",
        }
        quiz, problems = _parse_quiz(json.dumps({
            "questions": [
                good_choice,
                {"type": "choice", "question": "bad options", "options": ["A"], "answer": "0"},
                {
                    "type": "choice",
                    "question": "bad answer",
                    "options": ["A", "B", "C", "D"],
                    "answer": "9",
                },
                {"type": "short", "question": "bad short", "answer": ""},
                {"type": "essay", "question": "bad type", "answer": "x"},
                "not a dict",
            ],
        }))
        assert len(quiz.questions) == 1
        assert quiz.questions[0].question == "good"
        assert len(problems) == 5

    def test_parse_accepts_int_answer(self):
        quiz, _ = _parse_quiz(json.dumps({
            "questions": [{
                "type": "choice",
                "question": "?",
                "options": ["A", "B", "C", "D"],
                "answer": 1,
            }],
        }))
        assert quiz.questions[0].answer == "1"

    def test_parse_accepts_letter_answer(self):
        quiz, _ = _parse_quiz(json.dumps({
            "questions": [{
                "type": "choice",
                "question": "?",
                "options": ["A", "B", "C", "D"],
                "answer": "C.",
            }],
        }))
        assert quiz.questions[0].answer == "2"

    def test_parse_empty_questions(self):
        quiz, problems = _parse_quiz('{"title": "T", "questions": []}')
        assert quiz.questions == []
        assert problems == []

    def test_parse_missing_questions_array(self):
        quiz, problems = _parse_quiz('{"title": "T"}')
        assert quiz.questions == []
        assert problems == ["缺少 questions 数组"]

    def test_parse_not_json(self):
        quiz, problems = _parse_quiz("完全不是 JSON")
        assert quiz.questions == []
        assert problems == ["输出不是有效 JSON"]

    def test_difficulty_normalized(self):
        quiz, _ = _parse_quiz(json.dumps({
            "questions": [{"type": "short", "question": "?", "answer": "a", "difficulty": "hard"}],
        }))
        assert quiz.questions[0].difficulty == "understand"

    def test_category_preserved(self):
        quiz, _ = _parse_quiz(json.dumps({
            "questions": [{
                "type": "short",
                "question": "?",
                "answer": "a",
                "category": "计算机网络",
            }],
        }))
        assert quiz.questions[0].category == "计算机网络"

    def test_category_defaults_empty(self):
        quiz, _ = _parse_quiz(json.dumps({
            "questions": [{"type": "short", "question": "?", "answer": "a"}],
        }))
        assert quiz.questions[0].category == ""


# ─── Topic quiz (画像 + 一句话主题) ───────────────────────────────


class TestTopicQuizMessages:
    def test_profile_block_included(self):
        msgs = build_topic_quiz_messages(
            {"major": "计算机", "grade": "大三", "goal": "考研", "note": ""},
            "TCP 三次握手",
            5,
            "mixed",
        )
        user = msgs[1]["content"]
        assert "专业：计算机" in user
        assert "年级：大三" in user
        assert "目标：考研" in user
        assert "补充：" not in user  # empty note skipped
        assert "出题主题：TCP 三次握手" in user

    def test_style_engineering_for_job_seekers(self):
        msgs = build_topic_quiz_messages({"goal": "求职后端开发"}, "Redis 持久化", 3, "short")
        assert "偏工程化" in msgs[1]["content"]

    def test_style_academic_for_exam(self):
        msgs = build_topic_quiz_messages({"goal": "考研（408）"}, "进程调度", 3, "short")
        assert "偏考试化" in msgs[1]["content"]

    def test_empty_profile_no_block(self):
        msgs = build_topic_quiz_messages({}, "行测言语理解", 3, "mixed")
        assert "用户画像" not in msgs[1]["content"]

    def test_progressive_and_category_instructions(self):
        msgs = build_topic_quiz_messages({}, "操作系统", 3, "mixed")
        user = msgs[1]["content"]
        assert "渐进展开" in user
        assert "category" in user


class TestGenerateTopicQuiz:
    def test_happy_path(self, monkeypatch):
        topic_quiz = json.dumps({
            "title": "TCP 专题",
            "questions": [{
                "type": "short",
                "category": "计算机网络",
                "question": "为什么需要三次握手？",
                "answer": "防止旧的连接请求到达",
                "difficulty": "understand",
            }],
        })
        fake = _FakeLLM([topic_quiz])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        quiz = generate_topic_quiz({"major": "计算机"}, "TCP 三次握手", 1, "short")
        assert len(quiz.questions) == 1
        assert quiz.questions[0].category == "计算机网络"
        assert "TCP 三次握手" in fake.calls[0]["messages"][1]["content"]


# ─── generate_quiz orchestration ──────────────────────────────────


@dataclass
class _Msg:
    content: str = ""


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Resp:
    choices: list


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages=None, temperature=0.7, response_format=None, **kw):
        self.calls.append({"messages": messages, "response_format": response_format})
        raw = self.responses.pop(0) if self.responses else "{}"
        return _Resp(choices=[_Choice(message=_Msg(content=raw))])


GOOD = json.dumps({
    "title": "T",
    "questions": [
        {"type": "short", "question": "Q1", "answer": "a"},
        {"type": "short", "question": "Q2", "answer": "b"},
    ],
})
BAD = '{"questions": [{"type": "choice", "question": "?", "options": ["A"], "answer": "9"}]}'


class TestGenerateQuiz:
    def test_happy_path_json_mode(self, monkeypatch):
        fake = _FakeLLM([GOOD])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        quiz = generate_quiz("材料", 2, "short")
        assert len(quiz.questions) == 2
        assert fake.calls[0]["response_format"] == {"type": "json_object"}
        # prompt contains the material
        assert "材料" in fake.calls[0]["messages"][1]["content"]

    def test_retry_with_problems(self, monkeypatch):
        fake = _FakeLLM([BAD, GOOD])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        quiz = generate_quiz("材料", 2, "short")
        assert len(quiz.questions) == 2
        assert len(fake.calls) == 2
        assert "重新输出" in fake.calls[1]["messages"][1]["content"]

    def test_llm_exception_raises(self, monkeypatch):
        class BoomLLM:
            def chat(self, **kw):
                raise RuntimeError("api down")

        monkeypatch.setattr(llm_module, "llm_client", BoomLLM(), raising=False)
        with pytest.raises(QuizGenError, match="api down"):
            generate_quiz("材料", 2, "short")

    def test_both_bad_raises(self, monkeypatch):
        fake = _FakeLLM([BAD, BAD])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        with pytest.raises(QuizGenError, match="有效题目"):
            generate_quiz("材料", 2, "short")

    def test_uses_best_attempt(self, monkeypatch):
        one_good = json.dumps({
            "questions": [{"type": "short", "question": "Q", "answer": "a"}]
        })
        fake = _FakeLLM([BAD, one_good])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        quiz = generate_quiz("材料", 2, "short", min_questions=1)
        assert len(quiz.questions) == 1


# ─── Grader ───────────────────────────────────────────────────────


class TestVerdict:
    def test_thresholds(self):
        assert verdict_of(1.0) == "right"
        assert verdict_of(0.8) == "right"
        assert verdict_of(0.79) == "partial"
        assert verdict_of(0.4) == "partial"
        assert verdict_of(0.39) == "wrong"
        assert verdict_of(0.0) == "wrong"


class TestGradeOne:
    def test_happy(self, monkeypatch):
        fake = _FakeLLM(['{"score": 0.7, "feedback": "漏了要点2"}'])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        res = grade_one("题目", "参考", "学生答案")
        assert res == {"score": 0.7, "verdict": "partial", "feedback": "漏了要点2"}
        assert fake.calls[0]["response_format"] == {"type": "json_object"}

    def test_score_clamped(self, monkeypatch):
        fake = _FakeLLM(['{"score": 2.5, "feedback": ""}'])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        assert grade_one("q", "r", "a")["score"] == 1.0

    def test_invalid_score_zero(self, monkeypatch):
        fake = _FakeLLM(['{"score": "high", "feedback": ""}'])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        assert grade_one("q", "r", "a")["score"] == 0.0

    def test_empty_answer_short_circuits(self, monkeypatch):
        fake = _FakeLLM([])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        res = grade_one("q", "r", "  ")
        assert res["score"] == 0.0
        assert res["verdict"] == "wrong"
        assert fake.calls == []

    def test_bad_json_raises(self, monkeypatch):
        fake = _FakeLLM(["not json"])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        with pytest.raises(grader_module.GradeError):
            grade_one("q", "r", "a")


class TestGradeBatch:
    def test_batch_parses_and_clamps(self, monkeypatch):
        fake = _FakeLLM([json.dumps({"results": [
            {"id": "q1", "score": 0.9, "feedback": "好"},
            {"id": "q2", "score": 5, "feedback": ""},
        ]})])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        items = [
            {"question_id": "q1", "question": "Q1", "ref_answer": "R1", "user_answer": "U1"},
            {"question_id": "q2", "question": "Q2", "ref_answer": "R2", "user_answer": "U2"},
        ]
        res = grade_batch(items)
        assert res["q1"]["score"] == 0.9
        assert res["q1"]["verdict"] == "right"
        assert res["q2"]["score"] == 1.0  # clamped
        # single LLM call
        assert len(fake.calls) == 1

    def test_missing_items_zero_filled(self, monkeypatch):
        fake = _FakeLLM([json.dumps({"results": [{"id": "q1", "score": 1.0}]})])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        items = [
            {"question_id": "q1", "question": "Q", "ref_answer": "R", "user_answer": "U"},
            {"question_id": "q2", "question": "Q", "ref_answer": "R", "user_answer": ""},
        ]
        res = grade_batch(items)
        assert res["q1"]["score"] == 1.0
        assert res["q2"]["score"] == 0.0
        assert "未返回" in res["q2"]["feedback"]

    def test_whole_batch_llm_failure_zero_fills(self, monkeypatch):
        class BoomLLM:
            def chat(self, **kw):
                raise RuntimeError("api down")

        monkeypatch.setattr(llm_module, "llm_client", BoomLLM(), raising=False)
        items = [{"question_id": "q1", "question": "Q", "ref_answer": "R", "user_answer": "U"}]
        res = grade_batch(items)
        assert res["q1"]["score"] == 0.0
        assert "不可用" in res["q1"]["feedback"]

    def test_empty_items_no_call(self, monkeypatch):
        fake = _FakeLLM([])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        assert grade_batch([]) == {}
        assert fake.calls == []

    def test_unknown_and_duplicate_ids_ignored(self, monkeypatch):
        fake = _FakeLLM([json.dumps({"results": [
            {"id": "ghost", "score": 1.0},
            {"id": "q1", "score": 0.5},
            {"id": "q1", "score": 1.0},  # duplicate — first wins
        ]})])
        monkeypatch.setattr(llm_module, "llm_client", fake, raising=False)
        items = [{"question_id": "q1", "question": "Q", "ref_answer": "R", "user_answer": "U"}]
        res = grade_batch(items)
        assert res["q1"]["score"] == 0.5
        assert "ghost" not in res
