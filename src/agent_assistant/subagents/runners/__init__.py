"""Role runners for the SubagentManager."""

from .explorer import explorer_runner
from .operator import operator_runner
from .organizer import organizer_runner
from .researcher import researcher_runner

__all__ = [
    "explorer_runner",
    "operator_runner",
    "organizer_runner",
    "researcher_runner",
]
