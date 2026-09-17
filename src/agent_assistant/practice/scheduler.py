"""Leitner box scheduler — pure functions, no I/O.

Five boxes with growing review intervals. A correct answer promotes the card
one box (capped at box 5); a wrong answer resets it to box 1. The due date is
``now + BOX_DAYS[new_box]`` days. Deliberately simple, explainable and testable
— good enough for personal study pacing without FSRS parameter tuning.
"""

from __future__ import annotations

import time

# Box → review interval in days
BOX_DAYS: dict[int, int] = {1: 1, 2: 2, 3: 4, 4: 7, 5: 15}

MIN_BOX = 1
MAX_BOX = 5

# A short answer scoring at/above this counts as "correct" for scheduling
# (matches the grader's verdict threshold: right ≥ 0.8, partial 0.4~0.8).
CORRECT_SCORE_THRESHOLD = 0.6


def clamp_box(box: int) -> int:
    return max(MIN_BOX, min(MAX_BOX, int(box)))


def next_state(
    box: int, correct: bool, now: float | None = None
) -> tuple[int, float]:
    """Advance a card's Leitner state.

    Returns ``(new_box, due_at)``. Correct → box+1 (capped); wrong → box 1.
    """
    ts = time.time() if now is None else now
    current = clamp_box(box)
    new_box = min(MAX_BOX, current + 1) if correct else MIN_BOX
    return new_box, ts + BOX_DAYS[new_box] * 86400


def is_due(due_at: float, now: float | None = None) -> bool:
    """单卡是否到期。

    注意：产品路径走 ``PracticeStore.due_cards()``（SQL 层批量判断，避免逐卡
    载入 Python）；本函数目前只有测试在用，保留是因为它是调度语义的可读表达，
    改主意要动之前请先确认没有新的调用方。
    """
    ts = time.time() if now is None else now
    return due_at <= ts
