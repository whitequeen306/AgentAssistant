"""Practice service — orchestration between the bridge and practice internals.

The ONLY write path: generate (persist quiz), submit (grade + attempts +
Leitner update), review/wrong-start (reuse stored questions), report export.
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from agent_assistant.practice.grader import grade_batch, grade_one
from agent_assistant.practice.quizgen import (
    QuizGenError,
    generate_quiz,
    generate_topic_quiz,
)
from agent_assistant.practice.scheduler import CORRECT_SCORE_THRESHOLD
from agent_assistant.practice.store import practice_store

logger = logging.getLogger(__name__)

MATERIAL_CAP = 24_000  # chars of material fed to the quiz LLM


def _load_note_material(source_id: str) -> tuple[str, str]:
    """Read a library note. Returns (material, title) or raises ValueError."""
    from agent_assistant.config import settings

    if not source_id or not isinstance(source_id, str):
        raise ValueError("缺少笔记文件名")
    name = source_id.replace("\\", "/").split("/")[-1].strip()
    if not name or name in (".", "..") or not name.endswith(".md"):
        raise ValueError("非法笔记文件名")
    path = (settings.resolved_notes_dir / name).resolve()
    if not str(path).startswith(str(settings.resolved_notes_dir.resolve())):
        raise ValueError("非法笔记路径")
    if not path.is_file():
        raise ValueError("笔记不存在")
    text = path.read_text(encoding="utf-8", errors="replace")
    title = _strip_frontmatter_title(text) or path.stem
    return text, title


def _load_local_file_material(path_str: str) -> tuple[str, str]:
    """Extract text from ANY local file (精读联动「就这篇，考我」）.

    Reuses read_file's extraction pipeline (PDF/Word/Excel/PPT/RTF/text).
    Gated by the same filesystem jail as file tools — the path comes from
    the chat's own read_file step, but we re-check rather than trust it.
    """
    from agent_assistant.tools.file_tools import (
        UnsupportedBinaryError,
        extract_file_text,
    )
    from agent_assistant.tools.permission import path_in_allowed_roots

    if not path_str or not path_str.strip():
        raise ValueError("缺少文件路径")
    path = Path(path_str.strip())
    if not path_in_allowed_roots(str(path)):
        raise ValueError("该文件在允许的工作区之外（可在设置中调整）")
    if not path.is_file():
        raise ValueError("文件不存在或已被移动")
    try:
        text, _meta = extract_file_text(path)
    except UnsupportedBinaryError as e:
        raise ValueError(f"不支持的文件格式: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"读取文件失败: {e}") from e
    return text, path.stem


def _load_kb_material(source_id: str) -> tuple[str, str]:
    """Read a knowledge-base file's ORIGINAL stored copy (full coverage).

    Falls back to chunk retrieval when the stored copy is missing.
    """
    from agent_assistant.knowledge.service import knowledge_service

    entry = knowledge_service.registry.get(source_id)
    if entry is None:
        raise ValueError("知识库文件不存在")

    material = ""
    stored = getattr(entry, "stored_path", "")
    if stored:
        try:
            from pathlib import Path

            material = Path(stored).read_text(encoding="utf-8", errors="replace")
        except OSError:
            material = ""
    if not material.strip():
        # Fallback: pull chunks for this file_id via retrieval.
        chunks = knowledge_service.retrieve(entry.title, n_results=16)
        parts = [
            c.get("content", "")
            for c in chunks
            if (c.get("file_id") or c.get("metadata", {}).get("file_id"))
            == source_id
        ]
        material = "\n\n".join(parts)
    if not material.strip():
        raise ValueError("知识库文件内容为空")
    return material, entry.title


def _strip_frontmatter_title(text: str) -> str:
    if not text.startswith("---"):
        return ""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    m = re.search(r'(?m)^title:\s*"?(.+?)"?\s*$', parts[1])
    return m.group(1).strip() if m else ""


class PracticeService:
    """Facade used by the UI bridge."""

    # ─── Generate ──────────────────────────────────────────────────

    def generate(
        self,
        source_kind: str,
        source_id: str,
        count: int = 5,
        qtype: str = "mixed",
        mode: str = "paper",
    ) -> dict[str, Any]:
        try:
            count = max(1, min(int(count), 10))
        except (TypeError, ValueError):
            count = 5

        if source_kind == "custom":
            return self._generate_from_topic(source_id, count, qtype, mode)

        try:
            if source_kind == "note":
                material, source_title = _load_note_material(source_id)
            elif source_kind == "kb":
                material, source_title = _load_kb_material(source_id)
            elif source_kind == "file":
                material, source_title = _load_local_file_material(source_id)
            else:
                return {"ok": False, "error": f"unknown source_kind: {source_kind}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"读取材料失败: {e}"}

        material = (material or "").strip()[:MATERIAL_CAP]
        if not material:
            return {"ok": False, "error": "材料内容为空，无法出题"}

        try:
            quiz = generate_quiz(material, count, qtype, min_questions=1)
        except QuizGenError as e:
            return {"ok": False, "error": str(e)}

        return self._persist_quiz(
            quiz, source_kind, source_id, source_title, mode
        )

    def _generate_from_topic(
        self, topic: str, count: int, qtype: str, mode: str
    ) -> dict[str, Any]:
        """画像 + 一句话主题出题（不依赖学习库材料）。"""
        topic = (topic or "").strip()[:200]
        if not topic:
            return {"ok": False, "error": "请输入出题主题"}
        try:
            from agent_assistant.ui.store import ui_store

            profile = {
                "major": (ui_store.get_setting("profile_major", "") or "").strip(),
                "grade": (ui_store.get_setting("profile_grade", "") or "").strip(),
                "goal": (ui_store.get_setting("profile_goal", "") or "").strip(),
                "note": (ui_store.get_setting("profile_note", "") or "").strip(),
            }
        except Exception:  # noqa: BLE001
            profile = {}
        try:
            quiz = generate_topic_quiz(profile, topic, count, qtype, min_questions=1)
        except QuizGenError as e:
            return {"ok": False, "error": str(e)}
        return self._persist_quiz(quiz, "custom", topic, topic, mode)

    def _persist_quiz(
        self,
        quiz: Any,
        source_kind: str,
        source_id: str,
        source_title: str,
        mode: str,
    ) -> dict[str, Any]:
        normalized_mode = mode if mode in ("paper", "card") else "paper"
        title = quiz.title or f"练习：{source_title}"
        sess = practice_store.create_session(
            source_kind=source_kind,
            source_id=source_id,
            source_title=source_title,
            title=title,
            mode=normalized_mode,
            question_count=len(quiz.questions),
        )
        import uuid as _uuid

        qids = [f"q{_uuid.uuid4().hex[:12]}" for _ in quiz.questions]
        for qid, q in zip(qids, quiz.questions):
            practice_store.add_question(
                question_id=qid,
                type=q.type,
                question=q.question,
                options=q.options,
                answer=q.answer,
                explain=q.explain,
                difficulty=q.difficulty,
                category=q.category,
            )
        practice_store.link_questions(sess["session_id"], qids)

        return {
            "ok": True,
            "session_id": sess["session_id"],
            "title": title,
            "source_title": source_title,
            "source_kind": source_kind,
            "mode": normalized_mode,
            "questions": [
                {
                    "id": qid,
                    "type": q.type,
                    "question": q.question,
                    "options": q.options,
                    "answer": q.answer,
                    "explain": q.explain,
                    "difficulty": q.difficulty,
                    "category": q.category,
                }
                for qid, q in zip(qids, quiz.questions)
            ],
        }

    # ─── Card-mode instant grading (NOT persisted) ─────────────────

    def grade_short(self, question_id: str, user_answer: str) -> dict[str, Any]:
        row = practice_store.get_question(question_id)
        if not row:
            return {"ok": False, "error": "题目不存在"}
        try:
            result = grade_one(row["question"], row["answer"], user_answer or "")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, **result}

    # ─── Submit ────────────────────────────────────────────────────

    def submit(
        self, session_id: str, answers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        session = practice_store.get_session(session_id)
        if not session:
            return {"ok": False, "error": "练习会话不存在"}
        if session.get("submitted"):
            return {"ok": False, "error": "该练习已提交过"}

        questions = {
            q["question_id"]: q
            for q in practice_store.get_session_questions(session_id)
        }
        answer_map = {
            str(a.get("question_id")): a for a in (answers or []) if isinstance(a, dict)
        }

        # 1) Local-grade choices; collect shorts needing LLM batch grading.
        graded: dict[str, dict[str, Any]] = {}
        pending_short: list[dict[str, str]] = []
        for qid, q in questions.items():
            a = answer_map.get(qid, {})
            user_answer = str(a.get("user_answer") or "")
            if "score" in a and a.get("score") is not None:
                # Frontend already graded (card mode passed through).
                try:
                    score = round(max(0.0, min(1.0, float(a["score"]))), 1)
                except (TypeError, ValueError):
                    score = 0.0
                graded[qid] = {
                    "user_answer": user_answer,
                    "score": score,
                    "feedback": str(a.get("feedback") or ""),
                }
            elif q["type"] == "choice":
                correct = user_answer.strip() == str(q["answer"]).strip()
                graded[qid] = {
                    "user_answer": user_answer,
                    "score": 1.0 if correct else 0.0,
                    "feedback": "",
                }
            else:
                pending_short.append(
                    {
                        "question_id": qid,
                        "question": q["question"],
                        "ref_answer": q["answer"],
                        "user_answer": user_answer,
                    }
                )

        # 2) Batch-grade remaining shorts in one LLM call.
        if pending_short:
            batch = grade_batch(pending_short)
            for qid, res in batch.items():
                graded[qid] = {
                    "user_answer": str(answer_map.get(qid, {}).get("user_answer") or ""),
                    "score": res["score"],
                    "feedback": res["feedback"],
                }

        # 3) Persist attempts + advance Leitner cards.
        per_question: list[dict[str, Any]] = []
        scores: list[float] = []
        correct_count = 0
        for qid in questions:
            g = graded.get(qid, {"user_answer": "", "score": 0.0, "feedback": ""})
            practice_store.insert_attempt(
                question_id=qid,
                session_id=session_id,
                user_answer=g["user_answer"],
                score=g["score"],
                feedback=g["feedback"],
            )
            practice_store.upsert_card_for_attempt(
                qid, correct=g["score"] >= CORRECT_SCORE_THRESHOLD
            )
            scores.append(g["score"])
            correct_count += 1 if g["score"] >= CORRECT_SCORE_THRESHOLD else 0
            per_question.append(
                {
                    "question_id": qid,
                    "score": g["score"],
                    "is_correct": g["score"] >= CORRECT_SCORE_THRESHOLD,
                    "verdict": (
                        "right"
                        if g["score"] >= 0.8
                        else "partial" if g["score"] >= 0.4 else "wrong"
                    ),
                    "feedback": g["feedback"],
                }
            )

        score_pct = round(sum(scores) / len(scores) * 100) if scores else 0
        practice_store.finalize_session(
            session_id, correct_count=correct_count, score_pct=score_pct
        )
        return {
            "ok": True,
            "score_pct": score_pct,
            "correct_count": correct_count,
            "total": len(questions),
            "per_question": per_question,
        }

    # ─── Review / wrong-retry ──────────────────────────────────────

    def due_info(self) -> dict[str, Any]:
        return practice_store.due_info()

    def dashboard(self) -> dict[str, Any]:
        return practice_store.dashboard()

    def start_review(self, limit: int = 20) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 30))
        except (TypeError, ValueError):
            limit = 20
        cards = practice_store.due_cards(limit=limit)
        if not cards:
            return {"ok": False, "error": "今天没有到期的复习卡片"}
        qids = [c["question_id"] for c in cards]
        sess = practice_store.create_session(
            source_kind="review",
            source_id="",
            source_title="复习",
            title="今日复习",
            mode="card",
            question_count=len(qids),
        )
        practice_store.link_questions(sess["session_id"], qids)
        return self._quiz_payload(sess["session_id"], "今日复习", "review", "card")

    def start_wrong(self, session_id: str) -> dict[str, Any]:
        wrong = practice_store.get_wrong_questions(session_id)
        if not wrong:
            return {"ok": False, "error": "该练习没有错题"}
        random.shuffle(wrong)
        source = practice_store.get_session(session_id) or {}
        qids = [q["question_id"] for q in wrong]
        sess = practice_store.create_session(
            source_kind="wrong",
            source_id=source.get("source_id", ""),
            source_title=source.get("source_title", "错题重练"),
            title=f"错题重练：{source.get('source_title', '')}",
            mode="card",
            question_count=len(qids),
        )
        practice_store.link_questions(sess["session_id"], qids)
        return self._quiz_payload(
            sess["session_id"],
            f"错题重练：{source.get('source_title', '')}",
            "wrong",
            "card",
        )

    def history(self, limit: int = 20) -> dict[str, Any]:
        return {"ok": True, "sessions": practice_store.list_sessions(limit)}

    def session_detail(self, session_id: str) -> dict[str, Any]:
        """Full replay payload of a submitted session (history detail view).

        Returns the session summary, its questions in quiz order and each
        question's attempt (user answer / score / feedback) so the frontend
        can re-render the exact post-submission state.
        """
        session = practice_store.get_session(session_id)
        if not session:
            return {"ok": False, "error": "练习会话不存在"}
        if not session.get("submitted"):
            return {"ok": False, "error": "该练习尚未提交"}
        questions = practice_store.get_session_questions(session_id)
        attempts = practice_store.get_session_attempts(session_id)
        return {
            "ok": True,
            "session": session,
            "questions": [
                {
                    "id": q["question_id"],
                    "type": q["type"],
                    "question": q["question"],
                    "options": q.get("options"),
                    "answer": q["answer"],
                    "explain": q.get("explain") or "",
                    "difficulty": q.get("difficulty") or "",
                }
                for q in questions
            ],
            "attempts": [
                {
                    "question_id": a["question_id"],
                    "user_answer": a.get("user_answer") or "",
                    "score": a.get("score", 0),
                    "is_correct": bool(a.get("is_correct")),
                    "feedback": a.get("feedback") or "",
                }
                for a in attempts
            ],
        }

    # ─── Report export ─────────────────────────────────────────────

    def save_report(self, session_id: str) -> dict[str, Any]:
        session = practice_store.get_session(session_id)
        if not session:
            return {"ok": False, "error": "练习会话不存在"}
        questions = practice_store.get_session_questions(session_id)
        wrong_ids: set[str] = set()
        for q in questions:
            attempts = self._last_attempt(session_id, q["question_id"])
            if attempts is not None and attempts["score"] < CORRECT_SCORE_THRESHOLD:
                wrong_ids.add(q["question_id"])

        lines = [
            f"# 练习报告：{session['title']}",
            "",
            f"- 来源：{session['source_title']}",
            f"- 成绩：{session.get('correct_count', 0)}/{session['question_count']}"
            f"（得分率 {session.get('score_pct', 0)}%）",
            f"- 日期：{time.strftime('%Y-%m-%d %H:%M')}",
            "",
        ]
        if not wrong_ids:
            lines += ["全部答对，没有错题。🎉"]
        else:
            lines += ["## 错题回顾", ""]
            for q in questions:
                if q["question_id"] not in wrong_ids:
                    continue
                att = self._last_attempt(session_id, q["question_id"]) or {}
                lines += [
                    f"### {q['question']}",
                    "",
                    f"- 你的答案：{att.get('user_answer') or '（未作答）'}",
                    f"- 得分：{att.get('score', 0)}",
                ]
                if att.get("feedback"):
                    lines.append(f"- AI 反馈：{att['feedback']}")
                lines += [
                    f"- 参考答案：{q['answer']}",
                    f"- 解析：{q.get('explain') or '（无）'}",
                    "",
                ]

        from agent_assistant.tools.save_note import SaveNoteTool

        result = SaveNoteTool().execute(
            title=f"练习报告：{session['title']}",
            content="\n".join(lines),
            tag="练习报告",
        )
        if result.ok:
            return {"ok": True, **(result.data or {})}
        return {"ok": False, "error": result.error or "保存失败"}

    # ─── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _last_attempt(session_id: str, question_id: str) -> dict[str, Any] | None:
        conn = practice_store._ensure_db()
        with practice_store._lock:
            row = conn.execute(
                "SELECT * FROM attempts WHERE session_id = ? AND question_id = ?"
                " ORDER BY answered_at DESC LIMIT 1",
                (session_id, question_id),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _quiz_payload(
        session_id: str, title: str, source_kind: str, mode: str
    ) -> dict[str, Any]:
        questions = practice_store.get_session_questions(session_id)
        return {
            "ok": True,
            "session_id": session_id,
            "title": title,
            "source_title": title,
            "source_kind": source_kind,
            "mode": mode,
            "questions": [
                {
                    "id": q["question_id"],
                    "type": q["type"],
                    "question": q["question"],
                    "options": q.get("options"),
                    "answer": q["answer"],
                    "explain": q.get("explain") or "",
                    "difficulty": q.get("difficulty") or "",
                    "category": q.get("category") or "",
                }
                for q in questions
            ],
        }


practice_service = PracticeService()
