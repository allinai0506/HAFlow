"""DecisionProvider layer tests: judge/score/choose mapping, Jev HTTP
contract handling (normal + failures), rule provider, registry."""

import json
import unittest

from herdr.decision import create_provider, provider_names
from herdr.decision.base import DecisionProvider
from herdr.decision.models import DecisionProviderError
from herdr.decision.providers.jev import JevDecisionProvider
from herdr.decision.providers.rule import RuleDecisionProvider


def _jev_transport(responses):
    """Fake transport; records requests, replays (status, body) or raises."""
    calls = []

    def post(url, headers, payload, timeout):
        calls.append({"url": url, "headers": dict(headers),
                      "payload": payload, "timeout": timeout})
        outcome = responses[len(calls) - 1] if len(calls) <= len(responses) else responses[-1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return post, calls


def _noul_body(value=0.92):
    return 200, json.dumps({
        "model": "jev-latest",
        "answers": {"q": {"type": "noul", "noul": value}},
        "usage": {"input_tokens": 312, "output_tokens": 48},
    })


class JevProviderTests(unittest.TestCase):
    def _provider(self, responses, key="JEV_TEST_KEY"):
        import os
        os.environ["JEV_API_KEY"] = key
        self.addCleanup(os.environ.pop, "JEV_API_KEY", None)
        post, calls = _jev_transport(responses)
        return JevDecisionProvider({"model": "jev-latest", "timeout": 5}, transport=post), calls

    def test_judge_maps_to_noul_without_fabricated_confidence(self):
        provider, calls = self._provider([_noul_body(0.86)])
        result = provider.judge("Did the agent make progress?", "state text")
        self.assertAlmostEqual(result.value, 0.86)
        self.assertIsNone(result.confidence, "noul has no confidence; must not fabricate")
        self.assertEqual(result.provider, "jev")
        self.assertIsNotNone(result.latency_ms)
        question = calls[0]["payload"]["questions"]["q"]
        self.assertEqual(question["type"], "noul")
        self.assertEqual(calls[0]["payload"]["model"], "jev-latest")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer JEV_TEST_KEY")

    def test_score_and_choose(self):
        score_body = 200, json.dumps({"answers": {
            "q": {"type": "score", "score": 1.6,
                  "legend": {"0": "a", "1": "b", "2": "c"},
                  "probabilities": {"0": 0.05, "1": 0.3, "2": 0.65},
                  "confidence": 0.78}}})
        provider, calls = self._provider([score_body])
        result = provider.score("How urgent?", {"s": 1}, ["can wait", "this week", "today"])
        self.assertAlmostEqual(result.value, 1.6)
        self.assertAlmostEqual(result.confidence, 0.78)
        self.assertEqual(calls[0]["payload"]["questions"]["q"]["criteria"],
                         ["can wait", "this week", "today"])

        choice_body = 200, json.dumps({"answers": {
            "q": {"type": "choice", "choice": "technical",
                  "probabilities": {"billing": 0.08, "technical": 0.85},
                  "confidence": 0.82}}})
        provider, calls = self._provider([choice_body])
        result = provider.choose("Which team?", "s", {"billing": None, "technical": "bugs"})
        self.assertEqual(result.value, "technical")
        self.assertAlmostEqual(result.probabilities["technical"], 0.85)

    def test_judge_many_batches_all_signals_in_one_request(self):
        answers = {
            name: {"type": "noul", "noul": 0.5}
            for name in ("worker_stuck", "needs_human", "ready_to_finish")
        }
        body = 200, json.dumps({"answers": answers})
        provider, calls = self._provider([body])
        results = provider.judge_many(
            {name: {"instructions": f"q {name}"} for name in answers},
            {"task_id": "t1"},
        )
        self.assertEqual(len(calls), 1, "one SupervisorState + N questions = one request")
        self.assertEqual(len(results), 3)
        self.assertEqual(calls[0]["payload"]["state"], {"task_id": "t1"})

    def test_judge_many_partial_response_drops_missing_signals(self):
        body = 200, json.dumps({"answers": {
            "a": {"type": "noul", "noul": 0.4},
            "b": {"type": "bogus"},
        }})
        provider, _ = self._provider([body])
        results = provider.judge_many(
            {"a": "x", "b": "y", "c": "z"}, {"s": 1})
        self.assertEqual(list(results), ["a"])

    def test_error_status_classification(self):
        cases = {401: "auth", 422: "invalid", 429: "rate_limit", 529: "overloaded"}
        for status, kind in cases.items():
            provider, _ = self._provider([(status, json.dumps({"detail": "x"}))])
            with self.assertRaises(DecisionProviderError) as ctx:
                provider.judge("q", "s")
            self.assertEqual(ctx.exception.kind, kind)

    def test_invalid_and_timeout_responses(self):
        provider, _ = self._provider([(200, "not json")])
        with self.assertRaises(DecisionProviderError) as ctx:
            provider.judge("q", "s")
        self.assertEqual(ctx.exception.kind, "invalid")

        provider, _ = self._provider([(200, json.dumps({"nope": 1}))])
        with self.assertRaises(DecisionProviderError) as ctx:
            provider.judge("q", "s")
        self.assertEqual(ctx.exception.kind, "invalid")

        provider, _ = self._provider([DecisionProviderError("timed out", kind="timeout")])
        with self.assertRaises(DecisionProviderError) as ctx:
            provider.judge("q", "s")
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_low_confidence_value_still_returned_verbatim(self):
        provider, _ = self._provider([_noul_body(0.51)])
        result = provider.judge("q", "s")
        self.assertAlmostEqual(result.value, 0.51)
        self.assertLess(result.certainty, 0.05)

    def test_missing_api_key_marks_unavailable(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("JEV_API_KEY", "TYPESAFE_API_KEY")}
        self.addCleanup(lambda: [os.environ.update({k: v}) for k, v in saved.items() if v])
        provider = JevDecisionProvider({})
        self.assertFalse(provider.available())
        with self.assertRaises(DecisionProviderError) as ctx:
            provider.judge("q", "s")
        self.assertEqual(ctx.exception.kind, "auth")


class RuleProviderTests(unittest.TestCase):
    def test_judge_many_uses_registered_rules(self):
        provider = RuleDecisionProvider(rules={"worker_stuck": lambda s: 0.9 if s.get("stuck") else 0.1})
        results = provider.judge_many({"worker_stuck": "stuck?", "unknown_q": "?"}, {"stuck": True})
        self.assertAlmostEqual(results["worker_stuck"].value, 0.9)
        self.assertNotIn("unknown_q", results)


class RegistryTests(unittest.TestCase):
    def test_builtin_providers_registered_and_creatable(self):
        self.assertIn("jev", provider_names())
        self.assertIn("rule", provider_names())
        self.assertIsInstance(create_provider("rule", {}), DecisionProvider)
        self.assertIsNone(create_provider("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
