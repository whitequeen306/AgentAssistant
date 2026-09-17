"""LLM short-answer grading — single question (card mode) and batch (paper).

Scores are semantic coverage in [0, 1] (one decimal). Verdict thresholds:
right ≥ 0.8, partial 0.4~0.8, wrong < 0.4. Choice questions never reach the
LLM — the frontend/bridge grades them locally against the stored index.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

GRADE_SYSTEM = (
    "你是严格但公正的阅卷老师，负责给学生练习题的简答题判分。"
    "语义等价即算对，不要求字面一致；错别字不扣分；"
    "只输出一个 JSON 对象，禁止 markdown 代码块、禁止任何解释文字。"
)


def _clamp_score(value: Any) -> float:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, s)), 1)


def verdict_of(score: float) -> str:
    if score >= 0.8:
        return "right"
    if score >= 0.4:
        return "partial"
    return "wrong"


def _extract_json(raw: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"```(?:json)?", "", raw or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class GradeError(Exception):
    pass


def grade_one(
    question: str, ref_answer: str, user_answer: str
) -> dict[str, Any]:
    """Grade one short answer. Returns {score, verdict, feedback}."""
    from agent_assistant.llm.client import llm_client

    if not (user_answer or "").strip():
        return {"score": 0.0, "verdict": "wrong", "feedback": "未作答"}

    user = (
        f"题目：{question}\n"
        f"参考答案要点：{ref_answer}\n"
        f"学生答案：{user_answer}\n\n"
        "按要点覆盖度给分（0~1，一位小数），并写一句话反馈"
        "（指出遗漏或肯定正确之处，不超过 60 字）。\n"
        '输出格式：{"score": 0.8, "feedback": "…"}'
    )
    try:
        response = llm_client.chat(
            messages=[
                {"role": "system", "content": GRADE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        raise GradeError(f"判分失败: {e}") from e

    data = _extract_json(raw)
    if data is None:
        raise GradeError("判分模型未返回有效 JSON")
    score = _clamp_score(data.get("score"))
    feedback = str(data.get("feedback") or "").strip()
    return {"score": score, "verdict": verdict_of(score), "feedback": feedback}


def grade_batch(
    items: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Grade many short answers in ONE LLM call.

    ``items``: [{"question_id", "question", "ref_answer", "user_answer"}].
    Returns {question_id: {score, verdict, feedback}}; items that fail to
    parse fall back to score 0 with an apologetic note (never raise — a
    single bad item must not sink the whole submission).
    """
    from agent_assistant.llm.client import llm_client

    if not items:
        return {}

    payload = [
        {
            "id": it["question_id"],
            "题目": it["question"],
            "参考答案要点": it["ref_answer"],
            "学生答案": (it["user_answer"] or "").strip() or "（未作答）",
        }
        for it in items
    ]
    user = (
        "以下是学生的一组简答题作答，请逐题按要点覆盖度判分"
        "（score 0~1，一位小数），每题写一句话反馈（不超过 60 字）。\n"
        '输出格式：{"results": [{"id": "题目id", "score": 0.8, "feedback": "…"}]}，'
        f"必须覆盖全部 {len(items)} 题。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )
    try:
        response = llm_client.chat(
            messages=[
                {"role": "system", "content": GRADE_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        # Whole-batch failure → zero-score everything with a note.
        logger.warning("grade_batch failed: %s", e)
        return {
            it["question_id"]: {
                "score": 0.0,
                "verdict": "wrong",
                "feedback": "AI 判分暂时不可用，请参考答案自评",
            }
            for it in items
        }

    data = _extract_json(raw) or {}
    results: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for row in data.get("results") or []:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("id") or "")
        if qid not in {it["question_id"] for it in items} or qid in seen:
            continue
        seen.add(qid)
        score = _clamp_score(row.get("score"))
        results[qid] = {
            "score": score,
            "verdict": verdict_of(score),
            "feedback": str(row.get("feedback") or "").strip(),
        }
    # Missing items → zero-score fallback (keeps submission atomic).
    for it in items:
        if it["question_id"] not in results:
            results[it["question_id"]] = {
                "score": 0.0,
                "verdict": "wrong",
                "feedback": "AI 未返回该题判分，请参考答案自评",
            }
    return results
