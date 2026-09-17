"""练习室（Practice Room）— 出题 / 判分 / 掌握度调度。

Modules:
- store:      SQLite persistence (sessions/questions/attempts/cards)
- scheduler:  pure-function Leitner box scheduling
- quizgen:    LLM quiz generation with schema validation + salvage retry
- grader:     LLM short-answer grading (single + batch)
- service:    orchestration — the only write path the bridge talks to
"""

from agent_assistant.practice.service import practice_service

__all__ = ["practice_service"]
