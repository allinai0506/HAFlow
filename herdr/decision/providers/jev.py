#!/usr/bin/env python3
"""Jev (TypeSafe System One) DecisionProvider (herdr/decision/providers/jev.py).

Maps HAFlow primitives onto the documented Jev HTTP contract
(POST /v1/systemone, questions of type noul/score/choice):

    judge  -> noul   (answer.noul, NO provider confidence -> confidence=None)
    score  -> score  (answer.score + probabilities + confidence)
    choose -> choice (answer.choice + probabilities + confidence)

A single ``judge_many`` call sends all questions in ONE request over one
bounded state. Standard library only (RULES: minimal dependencies); the
transport is injectable so tests and alternative deployments never touch
the network. API key comes from JEV_API_KEY (fallback TYPESAFE_API_KEY) and
is never persisted or logged: it is resolved from the environment at request
time, so removing the variable stops traffic immediately even while the
controller process keeps running.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Sequence, Tuple, Union

from ..base import DecisionProvider, Question, normalize_question
from ..models import DecisionProviderError, DecisionResult

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
SYSTEMONE_PATH = "/v1/systemone"

_STATE_TYPES = (str, dict, list)


def resolve_api_key(config: Optional[dict] = None) -> Optional[str]:
    cfg = config or {}
    for env_name in (cfg.get("api_key_env") or "JEV_API_KEY", "JEV_API_KEY", "TYPESAFE_API_KEY"):
        value = os.environ.get(str(env_name), "").strip()
        if value:
            return value
    return None


def _status_to_kind(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status == 529:
        return "overloaded"
    return "invalid"


def _http_post_json(url: str, headers: Dict[str, str], payload: dict,
                    timeout: float) -> Tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise DecisionProviderError("jev request timed out", kind="timeout") from exc
        raise DecisionProviderError(f"jev unreachable: {reason}", kind="unavailable") from exc
    except TimeoutError as exc:
        raise DecisionProviderError("jev request timed out", kind="timeout") from exc


class JevDecisionProvider(DecisionProvider):
    name = "jev"

    def __init__(self, config: Optional[dict] = None, transport=None) -> None:
        cfg = dict(config or {})
        self.base_url = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.model = cfg.get("model") or DEFAULT_MODEL
        self.timeout = float(cfg.get("timeout") or 20)
        self.enabled = bool(cfg.get("enabled", True))
        self._api_key_env = cfg.get("api_key_env")
        self._post = transport or _http_post_json

    def _resolve_api_key(self) -> Optional[str]:
        """Read the key from the environment now; never cache a secret."""
        config = {"api_key_env": self._api_key_env} if self._api_key_env else None
        return resolve_api_key(config)

    def available(self) -> bool:
        return bool(self.enabled) and bool(self._resolve_api_key())

    # ---------------------------------------------------------- request core

    def _ask(self, questions: Dict[str, dict],
             state: Union[str, dict, list]) -> Tuple[Dict[str, dict], float, dict]:
        if not self.enabled:
            raise DecisionProviderError("jev provider disabled", kind="auth")
        api_key = self._resolve_api_key()
        if not api_key:
            raise DecisionProviderError("jev api key not configured", kind="auth")
        if not isinstance(state, _STATE_TYPES):
            raise DecisionProviderError("state must be str/dict/list", kind="invalid")
        payload = {
            "state": state,
            "model": self.model,
            "questions": questions,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        status, body = self._post(
            self.base_url + SYSTEMONE_PATH, headers, payload, self.timeout
        )
        latency_ms = round((time.monotonic() - started) * 1000.0, 1)
        if status != 200:
            raise DecisionProviderError(
                f"jev http {status}", kind=_status_to_kind(status)
            )
        try:
            data = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise DecisionProviderError("jev response is not JSON", kind="invalid") from exc
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise DecisionProviderError("jev response missing answers", kind="invalid")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return answers, latency_ms, usage

    @staticmethod
    def _question_body(question: Question, qtype: str, criteria: Any) -> dict:
        body = normalize_question(question)
        body["type"] = qtype
        if criteria is not None:
            body["criteria"] = criteria
        return body

    @staticmethod
    def _number_or_none(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number

    def _result(self, answer: dict, latency_ms: float, usage: dict) -> DecisionResult:
        atype = answer.get("type")
        metadata: Dict[str, Any] = {"answer_type": atype}
        if usage:
            metadata["usage"] = dict(usage)
        common = {"provider": self.name, "latency_ms": latency_ms, "metadata": metadata}
        if atype == "noul":
            value = self._number_or_none(answer.get("noul"))
            if value is None:
                raise DecisionProviderError("jev noul answer malformed", kind="invalid")
            # Noul carries no confidence field; do not fabricate one.
            return DecisionResult(value=value, **common)
        if atype == "score":
            value = self._number_or_none(answer.get("score"))
            if value is None:
                raise DecisionProviderError("jev score answer malformed", kind="invalid")
            probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else None
            return DecisionResult(
                value=value,
                confidence=self._number_or_none(answer.get("confidence")),
                probabilities=probs,
                metadata={**metadata, "legend": answer.get("legend")},
                **{k: v for k, v in common.items() if k != "metadata"},
            )
        if atype == "choice":
            value = answer.get("choice")
            if not isinstance(value, str):
                raise DecisionProviderError("jev choice answer malformed", kind="invalid")
            probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else None
            return DecisionResult(
                value=value,
                confidence=self._number_or_none(answer.get("confidence")),
                probabilities=probs,
                **common,
            )
        raise DecisionProviderError(f"unknown jev answer type: {atype!r}", kind="invalid")

    # ------------------------------------------------------------ primitives

    def judge(self, question: Question, state: Union[str, dict, list]) -> DecisionResult:
        body = normalize_question(question)
        criteria = body.pop("criteria", None)
        answers, latency, usage = self._ask(
            {"q": {"type": "noul", **body, **({"criteria": criteria} if criteria else {})}},
            state,
        )
        return self._result(answers["q"], latency, usage)

    def score(self, question: Question, state: Union[str, dict, list],
              levels: Sequence[str]) -> DecisionResult:
        body = normalize_question(question)
        body.pop("criteria", None)
        answers, latency, usage = self._ask(
            {"q": self._question_body(body, "score", list(levels))}, state
        )
        return self._result(answers["q"], latency, usage)

    def choose(self, question: Question, state: Union[str, dict, list],
               options: Dict[str, Optional[str]]) -> DecisionResult:
        body = normalize_question(question)
        body.pop("criteria", None)
        answers, latency, usage = self._ask(
            {"q": self._question_body(body, "choice", dict(options))}, state
        )
        return self._result(answers["q"], latency, usage)

    def judge_many(self, questions: Dict[str, Question],
                   state: Union[str, dict, list]) -> Dict[str, DecisionResult]:
        """All noul questions in ONE request; per-question failures are dropped."""
        if not questions:
            return {}
        request = {
            question_id: self._question_body(normalize_question(question), "noul", None)
            for question_id, question in questions.items()
        }
        answers, latency, usage = self._ask(request, state)
        results: Dict[str, DecisionResult] = {}
        for question_id in questions:
            answer = answers.get(question_id)
            if not isinstance(answer, dict):
                continue  # partial response: missing signals stay absent
            try:
                results[question_id] = self._result(answer, latency, usage)
            except DecisionProviderError:
                continue
        return results
