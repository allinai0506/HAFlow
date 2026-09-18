#!/usr/bin/env python3
"""DecisionProvider abstraction (herdr/decision/base.py).

One narrow interface shields HAFlow from any specific judgment backend:

    judge  -> binary judgment, probability that the condition holds
    score  -> graded rating along a described scale
    choose -> pick one option from a defined set

``judge_many`` evaluates several independent questions over one state in a
single round-trip when the provider supports batching; the default loops
serially so every provider stays usable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Sequence, Union

from .models import DecisionResult

# A question is the instruction text, or {"instructions": str, "criteria": any}.
Question = Union[str, Dict[str, object]]


def normalize_question(question: Question) -> Dict[str, object]:
    if isinstance(question, dict):
        return dict(question)
    return {"instructions": str(question)}


class DecisionProvider(ABC):
    """Pluggable source of semantic judgments. Never mutates HAFlow state."""

    name = "base"

    def available(self) -> bool:
        """Cheap check: is this provider usable right now (key configured etc.)."""
        return True

    @abstractmethod
    def judge(self, question: Question, state: Union[str, dict, list]) -> DecisionResult:
        """Probability that a yes/no condition holds for ``state``."""

    @abstractmethod
    def score(self, question: Question, state: Union[str, dict, list],
              levels: Sequence[str]) -> DecisionResult:
        """Rate ``state`` along an ordered rubric of ``levels``."""

    @abstractmethod
    def choose(self, question: Question, state: Union[str, dict, list],
               options: Dict[str, Optional[str]]) -> DecisionResult:
        """Select one key of ``options`` (key -> rubric or None)."""

    def judge_many(self, questions: Dict[str, Question],
                   state: Union[str, dict, list]) -> Dict[str, DecisionResult]:
        """Judge independent questions over one state.

        Returns results keyed by question id; questions the provider could
        not answer are simply absent (partial failure is the caller's job to
        handle fail-safe).
        """
        results: Dict[str, DecisionResult] = {}
        for question_id, question in questions.items():
            try:
                results[question_id] = self.judge(question, state)
            except Exception:
                continue
        return results
