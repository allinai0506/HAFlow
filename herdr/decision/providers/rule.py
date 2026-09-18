#!/usr/bin/env python3
"""Rule-based DecisionProvider (herdr/decision/providers/rule.py).

A deterministic, offline provider: each question id is answered by a pure
rule function over the structured state. Doubles as the registry's network-
free reference implementation and as the test/CI stand-in for Jev.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence, Union

from ..base import DecisionProvider, Question, normalize_question
from ..models import DecisionProviderError, DecisionResult

Rule = Callable[[dict], float]


class RuleDecisionProvider(DecisionProvider):
    name = "rule"

    def __init__(self, config: Optional[dict] = None, rules: Optional[Dict[str, Rule]] = None) -> None:
        cfg = dict(config or {})
        self._rules: Dict[str, Rule] = dict(rules or cfg.get("rules") or {})

    def register(self, question_id: str, rule: Rule) -> None:
        self._rules[question_id] = rule

    def _ask(self, question_id: str, state: Union[str, dict, list]) -> DecisionResult:
        rule = self._rules.get(question_id)
        if rule is None:
            raise DecisionProviderError(f"no rule for question {question_id!r}", kind="unavailable")
        if not isinstance(state, dict):
            raise DecisionProviderError("rule provider expects dict state", kind="invalid")
        return DecisionResult(value=float(rule(state)), provider=self.name, latency_ms=0.0)

    def judge(self, question: Question, state: Union[str, dict, list]) -> DecisionResult:
        body = normalize_question(question)
        return self._ask(str(body.get("id") or body.get("instructions")), state)

    def score(self, question: Question, state: Union[str, dict, list],
              levels: Sequence[str]) -> DecisionResult:
        return self.judge(question, state)

    def choose(self, question: Question, state: Union[str, dict, list],
               options: Dict[str, Optional[str]]) -> DecisionResult:
        return self.judge(question, state)

    def judge_many(self, questions: Dict[str, Question],
                   state: Union[str, dict, list]) -> Dict[str, DecisionResult]:
        results: Dict[str, DecisionResult] = {}
        for question_id, question in questions.items():
            body = normalize_question(question)
            body["id"] = question_id
            try:
                results[question_id] = self.judge(body, state)
            except DecisionProviderError:
                continue
        return results
