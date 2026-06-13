"""SENTINEL agents (architecture.md §4)."""

from sentinel.agents.adversary_agent import AdversaryAgent
from sentinel.agents.arbitrator import Arbitrator
from sentinel.agents.baseline_agent import BaselineAgent
from sentinel.agents.lessons_agent import LessonsAgent
from sentinel.agents.yield_agent import YieldAgent

__all__ = [
    "AdversaryAgent",
    "Arbitrator",
    "BaselineAgent",
    "LessonsAgent",
    "YieldAgent",
]
