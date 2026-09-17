"""Practice store CRUD + Leitner scheduler transitions."""

from __future__ import annotations

import time

import pytest

from agent_assistant.practice.scheduler import (
    BOX_DAYS,
    MAX_BOX,
    MIN_BOX,
    clamp_box,
    is_due,
    next_state,
)
from agent_assistant.practice.store import PracticeStore


@pytest.fixture()
def store(tmp_path):
    s = PracticeStore(tmp_path / "practice.db")
    yield s
    s.close()


@pytest.fixture()
def seeded_session(store):
    """A submitted session with 2 questions (1 choice, 1 short)."""
    sess = store.create_session(
        source_kind="note",
        source_id="a.md",
        source_title="笔记A",
        title="练习：笔记A",
        question_count=2,
    )
    store.add_question(
        question_id="q1",
        type="choice",
        question="1+1=?",
        options=["A. 1", "B. 2", "C. 3", "D. 4"],
        answer="1",
        explain="显然",
        difficulty="basic",
    )
    store.add_question(
        question_id="q2",
        type="short",
        question="解释牛顿第二定律",
        options=None,
        answer="F=ma",
        explain="教材原文",
        difficulty="understand",
    )
    store.link_questions(sess["session_id"], ["q1", "q2"])
    return sess["session_id"]


# ─── Scheduler ────────────────────────────────────────────────────


class TestScheduler:
    def test_correct_promotes_box(self):
        box, due = next_state(2, True, now=1000.0)
        assert box == 3
        assert due == 1000.0 + BOX_DAYS[3] * 86400

    def test_wrong_resets_to_box_one(self):
        box, due = next_state(4, False, now=1000.0)
        assert box == MIN_BOX
        assert due == 1000.0 + BOX_DAYS[1] * 86400

    def test_correct_caps_at_max(self):
        box, _ = next_state(MAX_BOX, True, now=0.0)
        assert box == MAX_BOX

    def test_intervals_grow(self):
        assert BOX_DAYS[1] < BOX_DAYS[2] < BOX_DAYS[3] < BOX_DAYS[4] < BOX_DAYS[5]

    def test_clamp_box(self):
        assert clamp_box(0) == 1
        assert clamp_box(99) == MAX_BOX
        assert clamp_box(3) == 3

    def test_is_due(self):
        assert is_due(1000.0, now=1000.0)
        assert is_due(999.0, now=1000.0)
        assert not is_due(1001.0, now=1000.0)


# ─── Store: sessions ──────────────────────────────────────────────


class TestSessions:
    def test_create_and_get(self, store):
        sess = store.create_session(
            source_kind="kb",
            source_id="f1",
            source_title="论文",
            title="练习：论文",
            mode="card",
            question_count=5,
        )
        got = store.get_session(sess["session_id"])
        assert got is not None
        assert got["source_kind"] == "kb"
        assert got["mode"] == "card"
        assert got["submitted"] == 0

    def test_get_missing_returns_none(self, store):
        assert store.get_session("nope") is None

    def test_finalize_marks_submitted(self, store, seeded_session):
        store.finalize_session(seeded_session, correct_count=1, score_pct=50)
        got = store.get_session(seeded_session)
        assert got["submitted"] == 1
        assert got["correct_count"] == 1
        assert got["score_pct"] == 50

    def test_list_sessions_only_submitted(self, store, seeded_session):
        assert store.list_sessions() == []
        store.finalize_session(seeded_session, correct_count=2, score_pct=100)
        sessions = store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == seeded_session


# ─── Store: questions ─────────────────────────────────────────────


class TestQuestions:
    def test_add_and_get(self, store):
        store.add_question(
            question_id="q1",
            type="choice",
            question="?",
            options=["A", "B", "C", "D"],
            answer="0",
            explain="e",
            difficulty="basic",
        )
        got = store.get_question("q1")
        assert got["options"] == ["A", "B", "C", "D"]
        assert got["answer"] == "0"

    def test_get_missing_returns_none(self, store):
        assert store.get_question("nope") is None

    def test_session_questions_ordered(self, store, seeded_session):
        qs = store.get_session_questions(seeded_session)
        assert [q["question_id"] for q in qs] == ["q1", "q2"]
        assert qs[0]["type"] == "choice"
        assert qs[1]["options"] is None

    def test_add_question_idempotent(self, store):
        for _ in range(2):
            store.add_question(
                question_id="q1",
                type="choice",
                question="?",
                options=None,
                answer="0",
                explain="",
            )
        assert store.get_question("q1") is not None

    def test_wrong_questions(self, store, seeded_session):
        store.insert_attempt(
            question_id="q1",
            session_id=seeded_session,
            user_answer="0",
            score=0.0,
        )
        store.insert_attempt(
            question_id="q2",
            session_id=seeded_session,
            user_answer="F=mg",
            score=0.8,
            feedback="基本正确",
        )
        wrong = store.get_wrong_questions(seeded_session)
        assert [q["question_id"] for q in wrong] == ["q1"]

    def test_wrong_questions_uses_last_attempt(self, store, seeded_session):
        # First wrong, then right — question should NOT be in wrong list.
        store.insert_attempt(
            question_id="q1",
            session_id=seeded_session,
            user_answer="0",
            score=0.0,
        )
        store.insert_attempt(
            question_id="q1",
            session_id=seeded_session,
            user_answer="1",
            score=1.0,
        )
        assert store.get_wrong_questions(seeded_session) == []


# ─── Store: cards ─────────────────────────────────────────────────


class TestCards:
    def test_first_attempt_creates_card(self, store, seeded_session):
        card = store.upsert_card_for_attempt("q1", correct=True)
        assert card["box"] == 2
        assert card["total_attempts"] == 1
        assert card["correct_attempts"] == 1
        assert card["due_at"] > time.time()

    def test_wrong_first_attempt_stays_box1(self, store, seeded_session):
        card = store.upsert_card_for_attempt("q1", correct=False)
        assert card["box"] == 1
        assert card["correct_attempts"] == 0

    def test_promotion_then_reset(self, store, seeded_session):
        store.upsert_card_for_attempt("q1", correct=True)
        store.upsert_card_for_attempt("q1", correct=True)
        card = store.upsert_card_for_attempt("q1", correct=False)
        assert card["box"] == 1
        assert card["total_attempts"] == 3
        assert card["correct_attempts"] == 2

    def test_due_cards_filters_and_orders(self, store, seeded_session):
        # q1 answered long ago (due), q2 answered now (not due for box 2+).
        store.upsert_card_for_attempt("q1", correct=False)  # box 1, due +1d
        store.upsert_card_for_attempt("q2", correct=True)  # box 2, due +2d
        # Force q1 due now by backdating.
        conn = store._ensure_db()
        conn.execute(
            "UPDATE cards SET due_at = ? WHERE question_id = 'q1'",
            (time.time() - 10,),
        )
        conn.commit()
        due = store.due_cards()
        assert [d["question_id"] for d in due] == ["q1"]
        assert due[0]["question"] == "1+1=?"  # joined question data

    def test_due_info(self, store, seeded_session):
        info = store.due_info()
        assert info["due_count"] == 0
        assert info["boxes"] == {}
        store.upsert_card_for_attempt("q1", correct=False)
        conn = store._ensure_db()
        conn.execute("UPDATE cards SET due_at = 0 WHERE question_id = 'q1'")
        conn.commit()
        info = store.due_info()
        assert info["due_count"] == 1
        assert info["boxes"] == {"1": 1}


# ─── Dashboard aggregation ────────────────────────────────────────


class TestDashboard:
    def test_empty_db_zero_values(self, store):
        d = store.dashboard()
        assert d["cards_total"] == 0
        assert d["attempts_total"] == 0
        assert d["correct_rate"] == 0
        assert d["streak_days"] == 0
        assert d["boxes"] == {}
        assert d["weak_spots"] == []
        # 14-day series is always present, zero-filled, ascending
        assert len(d["last_14_days"]) == 14
        assert all(x["count"] == 0 for x in d["last_14_days"])
        assert d["last_14_days"][-1]["day"] == time.strftime("%Y-%m-%d")

    def test_aggregates_after_activity(self, store, seeded_session):
        store.insert_attempt(
            question_id="q1", session_id=seeded_session,
            user_answer="0", score=0.0,
        )
        store.insert_attempt(
            question_id="q1", session_id=seeded_session,
            user_answer="1", score=1.0,
        )
        store.insert_attempt(
            question_id="q2", session_id=seeded_session,
            user_answer="F=ma", score=0.8, feedback="ok",
        )
        store.upsert_card_for_attempt("q1", correct=False)
        store.upsert_card_for_attempt("q2", correct=True)

        d = store.dashboard()
        assert d["cards_total"] == 2
        assert d["attempts_total"] == 3
        # correct rate = is_correct avg: (0 + 1 + 1) / 3
        assert d["correct_rate"] == 67
        # q1: wrong → box 1; q2: right → box 2
        assert d["boxes"] == {"1": 1, "2": 1}
        assert d["mastered"] == 0
        assert d["streak_days"] >= 1
        # last day of series = today with 3 attempts
        last = d["last_14_days"][-1]
        assert last["count"] == 3
        assert last["correct"] == 2

    def test_weak_spots_filter_and_order(self, store, seeded_session):
        # q1: 2 attempts avg 0.25 (weak); q2: 1 attempt only (excluded).
        store.insert_attempt(
            question_id="q1", session_id=seeded_session,
            user_answer="0", score=0.0,
        )
        store.insert_attempt(
            question_id="q1", session_id=seeded_session,
            user_answer="0", score=0.5,
        )
        store.insert_attempt(
            question_id="q2", session_id=seeded_session,
            user_answer="F=ma", score=0.9,
        )
        d = store.dashboard()
        assert len(d["weak_spots"]) == 1
        w = d["weak_spots"][0]
        assert w["question_id"] == "q1"
        assert w["avg_score"] == 0.25
        assert w["attempts"] == 2
        assert w["question"] == "1+1=?"

    def test_streak_counts_back_from_today(self, store, seeded_session):
        # Practice "today" only → streak 1.
        store.insert_attempt(
            question_id="q1", session_id=seeded_session,
            user_answer="1", score=1.0,
        )
        assert store.dashboard()["streak_days"] == 1
        # Backdate one attempt to yesterday → streak 2.
        conn = store._ensure_db()
        yesterday = time.time() - 86400
        conn.execute(
            "INSERT INTO attempts (attempt_id, question_id, session_id,"
            " answered_at, user_answer, score, is_correct)"
            " VALUES ('x1', 'q1', ?, ?, '1', 1.0, 1)",
            (seeded_session, yesterday),
        )
        conn.commit()
        assert store.dashboard()["streak_days"] == 2
