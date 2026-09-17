"""LLM quiz generation with strict schema validation and salvage retry.

Pipeline: JSON-mode LLM call → pydantic validation → per-question salvage
(drop invalid questions) → if too few survive, one retry that feeds the
validation errors back into the prompt → raise if still insufficient.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


class QuizQuestion(BaseModel):
    """One validated quiz question."""

    model_config = ConfigDict(extra="ignore")

    type: str  # "choice" | "short"
    question: str
    options: list[str] | None = None
    # Declared Any: models sometimes emit int answers (0-3) — the validator
    # normalizes to a canonical str ("0".."3" for choice, text for short).
    answer: Any
    explain: str = ""
    difficulty: str = ""
    category: str = ""  # 知识点类别（自定义出题模式；材料模式可空）

    @field_validator("type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("choice", "short"):
            raise ValueError(f"unknown question type: {v!r}")
        return v

    @field_validator("question")
    @classmethod
    def _check_question(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("empty question")
        return v.strip()

    @field_validator("options")
    @classmethod
    def _check_options(cls, v: list[str] | None, info) -> list[str] | None:
        if info.data.get("type") != "choice":
            return None
        if not v or len(v) != 4:
            raise ValueError("choice questions need exactly 4 options")
        return [str(o) for o in v]

    @field_validator("answer")
    @classmethod
    def _check_answer(cls, v: Any, info) -> str:
        v = str(v if v is not None else "").strip()
        if info.data.get("type") == "choice":
            # Accept int-typed answers (0-3) or "A."-"D." style, normalize to index.
            idx = _normalize_choice_answer(v)
            if idx is None:
                raise ValueError(f"choice answer out of range: {v!r}")
            return str(idx)
        if not v:
            raise ValueError("short answer must not be empty")
        return v

    @field_validator("difficulty")
    @classmethod
    def _check_difficulty(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return v if v in ("basic", "understand", "apply") else "understand"


class Quiz(BaseModel):
    title: str = ""
    questions: list[QuizQuestion] = Field(default_factory=list)


def _normalize_choice_answer(raw: str) -> int | None:
    """Map various answer spellings onto 0..3, or None if invalid."""
    s = raw.strip()
    # "0".."3"
    if re.fullmatch(r"[0-3]", s):
        return int(s)
    # "A"/"A."/"A、…" style
    m = re.fullmatch(r"([A-Da-d])[.、．]?", s)
    if m:
        return "ABCD".index(m.group(1).upper())
    return None


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON extraction: strip fences, slice outermost object."""
    cleaned = re.sub(r"```(?:json)?", "", raw or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_quiz(raw: str) -> tuple[Quiz, list[str]]:
    """Parse LLM output into a Quiz; salvage each valid question individually.

    Returns (quiz, problems) where problems describe dropped/invalid items —
    fed back into the retry prompt.
    """
    data = _extract_json(raw)
    if data is None:
        return Quiz(), ["输出不是有效 JSON"]
    problems: list[str] = []
    questions: list[QuizQuestion] = []
    raw_qs = data.get("questions")
    if not isinstance(raw_qs, list):
        return Quiz(), ["缺少 questions 数组"]
    for i, item in enumerate(raw_qs):
        if not isinstance(item, dict):
            problems.append(f"第{i + 1}题不是对象")
            continue
        try:
            questions.append(QuizQuestion.model_validate(item))
        except Exception as e:  # noqa: BLE001 — salvage: drop bad item
            problems.append(f"第{i + 1}题无效: {e}")
    title = str(data.get("title") or "").strip()
    return Quiz(title=title, questions=questions), problems


def build_quiz_messages(
    material: str,
    count: int,
    qtype: str,
    *,
    problems: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build (system, user) messages; problems turn into a corrective retry."""
    type_line = {
        "choice": "全部为四选一选择题",
        "short": "全部为简答题",
    }.get(qtype, "选择题与简答题混合（约各半）")

    system = (
        "你是严谨的出题助手。根据给定学习材料出题用于复习自测。"
        "只输出一个 JSON 对象，禁止 markdown 代码块、禁止任何解释文字。"
    )
    user = (
        f"学习材料：\n<<<材料开始>>>\n{material}\n<<<材料结束>>>\n\n"
        f"请出 {count} 道题（{type_line}），覆盖材料的核心知识点，"
        "难度分布：基础(basic) 40% / 理解(understand) 40% / 应用(apply) 20%。\n"
        "硬性要求：选择题必须恰好 4 个选项；选择题 answer 必须是正确选项的"
        "索引（0-3 的数字字符串）；简答题 answer 为参考答案字符串（非空）；"
        "explain 引用材料原句；每题给出 difficulty（basic/understand/apply）。\n"
        '输出格式：{"title": "练习标题", "questions": ['
        '{"type": "choice", "question": "题干", '
        '"options": ["A. …", "B. …", "C. …", "D. …"], '
        '"answer": "0", "explain": "解析（引用材料原句）", "difficulty": "basic"}, '
        '{"type": "short", "question": "题干", '
        '"answer": "参考答案要点", "explain": "对应材料位置/原句", '
        '"difficulty": "understand"}]}。'
    )
    if problems:
        user += (
            "\n\n上一次输出存在以下问题，务必修正后重新输出完整 JSON：\n- "
            + "\n- ".join(problems)
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


QUIZ_SYSTEM = (
    "你是严谨的出题助手。根据给定学习材料或主题出题用于复习自测。"
    "只输出一个 JSON 对象，禁止 markdown 代码块、禁止任何解释文字。"
)

# 每题 JSON 字段说明（两种出题模式共用）
_QUESTION_SPEC = (
    "硬性要求：选择题必须恰好 4 个选项；选择题 answer 必须是正确选项的"
    "索引（0-3 的数字字符串）；简答题 answer 为参考答案字符串（非空）；"
    "每题给出 difficulty（basic/understand/apply）。\n"
    '输出格式：{"title": "练习标题", "questions": ['
    '{"type": "choice", "category": "知识点类别", "question": "题干", '
    '"options": ["A. …", "B. …", "C. …", "D. …"], '
    '"answer": "0", "explain": "解析", "difficulty": "basic"}, '
    '{"type": "short", "category": "知识点类别", "question": "题干", '
    '"answer": "参考答案要点", "explain": "解析", "difficulty": "understand"}]}。'
)


def _style_line(goal: str) -> str:
    """出题风格随用户目标自适应（求职 → 工程实战；考试 → 学术考卷）。"""
    g = goal or ""
    if any(k in g for k in ("求职", "工作", "面试", "八股", "实习", "校招", "社招")):
        return (
            "出题风格：偏工程化、偏实战——用真实工程场景提问，"
            "考察踩坑经验、排查思路、最佳实践与取舍，而不是背概念。"
        )
    if any(k in g for k in ("考研", "考公", "保研", "期末", "考试", "考编", "国考", "省考")):
        return (
            "出题风格：偏考试化、偏学术性——注重概念辨析、原理推导与教材式表述，"
            "题目措辞贴近真题卷面。"
        )
    return "出题风格：兼顾概念理解与实际应用。"


def build_topic_quiz_messages(
    profile: dict[str, str],
    topic: str,
    count: int,
    qtype: str,
    *,
    problems: list[str] | None = None,
) -> list[dict[str, str]]:
    """画像 + 用户一句话主题出题：抓 1 个核心知识点 + 类别，渐进展开。"""
    type_line = {
        "choice": "全部为四选一选择题",
        "short": "全部为简答题",
    }.get(qtype, "选择题与简答题混合（约各半）")

    profile_lines = []
    if profile.get("major"):
        profile_lines.append(f"专业：{profile['major']}")
    if profile.get("grade"):
        profile_lines.append(f"年级：{profile['grade']}")
    if profile.get("goal"):
        profile_lines.append(f"目标：{profile['goal']}")
    if profile.get("note"):
        profile_lines.append(f"补充：{profile['note']}")
    profile_block = (
        "用户画像：\n" + "\n".join(f"- {x}" for x in profile_lines) + "\n\n"
        if profile_lines
        else ""
    )

    user = (
        f"{profile_block}"
        f"出题主题：{topic}\n\n"
        f"请围绕该主题出 {count} 道题（{type_line}）。要求：\n"
        "1. 从主题中提炼 1 个核心知识点（用户已指明知识点则以它为准），"
        "并归纳其所属类别（如：计算机网络 / 操作系统 / 考研政治 / 行测言语理解），"
        "每题的 category 填该类别。\n"
        "2. 围绕这个知识点渐进展开：由浅入深、由概念到应用，题目之间有梯度。\n"
        f"3. {_style_line(profile.get('goal', ''))}\n"
        "4. 解析 explain 说明考点或易错点。\n"
        + _QUESTION_SPEC
    )
    if problems:
        user += (
            "\n\n上一次输出存在以下问题，务必修正后重新输出完整 JSON：\n- "
            + "\n- ".join(problems)
        )
    return [
        {"role": "system", "content": QUIZ_SYSTEM},
        {"role": "user", "content": user},
    ]


class QuizGenError(Exception):
    """Raised when no valid quiz can be produced after retry."""


def _generate_with_messages(
    messages_builder,
    count: int,
    min_questions: int,
) -> Quiz:
    """Shared retry engine: build → LLM (JSON mode) → validate → salvage.

    ``messages_builder(problems|None) -> [messages]`` so the corrective retry
    can feed validation problems back into the prompt.
    """
    from agent_assistant.llm.client import llm_client

    messages = messages_builder(None)
    best: Quiz | None = None

    for attempt in (1, 2):
        try:
            response = llm_client.chat(
                messages=messages,
                temperature=0.4,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            raise QuizGenError(f"出题失败: {e}") from e

        quiz, problems = _parse_quiz(raw)
        if len(quiz.questions) >= max(1, min_questions):
            return quiz
        best = quiz if (best is None or len(quiz.questions) > len(best.questions)) else best
        if attempt == 1:
            logger.info("Quiz retry: %d valid, problems=%s", len(quiz.questions), problems[:5])
            messages = messages_builder(problems or ["题目数量不足"])

    raise QuizGenError(
        f"模型未能生成足够数量的有效题目（{len(best.questions) if best else 0}/{count}），请重试"
    )


def generate_quiz(
    material: str,
    count: int,
    qtype: str,
    *,
    min_questions: int = 1,
) -> Quiz:
    """Generate a quiz from study material. One corrective retry on failure.

    Raises QuizGenError when fewer than ``min_questions`` valid questions
    survive both attempts.
    """
    return _generate_with_messages(
        lambda problems: build_quiz_messages(material, count, qtype, problems=problems),
        count,
        min_questions,
    )


def generate_topic_quiz(
    profile: dict[str, str],
    topic: str,
    count: int,
    qtype: str,
    *,
    min_questions: int = 1,
) -> Quiz:
    """Generate a quiz from the study profile + a one-line user topic."""
    return _generate_with_messages(
        lambda problems: build_topic_quiz_messages(
            profile, topic, count, qtype, problems=problems
        ),
        count,
        min_questions,
    )
