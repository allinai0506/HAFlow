"""SemanticSupervisor tests: bounded/redacted SupervisorState, batched
evaluation, persistence via events, previous-evaluation deltas, and the
continuous-evaluation rate controls (cooldown / budget / aggregation)."""

import json
import time
import unittest

from herdr.decision.models import DecisionResult
from herdr.supervisor.config import DEFAULTS, load_config
from herdr.supervisor.engine import RateGate, SemanticSupervisor
from herdr.supervisor.evaluation import (
    EVALUATION_EVENT,
    build_evaluation,
    evaluation_deltas,
    latest_evaluation,
)
from herdr.supervisor.harness import run_checkpoint
from herdr.supervisor.signals import SIGNAL_NAMES, signal_questions
from herdr.supervisor.state import build_supervisor_state, redact_text

ALL_SIGNALS = {
    "meaningful_progress": 0.86,
    "worker_stuck": 0.12,
    "work_off_track": 0.18,
    "requirements_satisfied": 0.71,
    "implementation_complete": 0.65,
    "tests_sufficient": 0.52,
    "needs_verification": 0.81,
    "needs_human": 0.09,
    "ready_to_finish": 0.43,
}


class StubProvider:
    name = "stub"

    def __init__(self, signals=None, raise_error=None):
        self.calls = 0
        self.states = []
        self.signals = dict(ALL_SIGNALS if signals is None else signals)
        self.raise_error = raise_error

    def available(self):
        return True

    def judge_many(self, questions, state):
        self.calls += 1
        self.states.append(state)
        if self.raise_error:
            raise self.raise_error
        return {
            name: DecisionResult(value=value, provider=self.name, latency_ms=42.0)
            for name, value in self.signals.items() if name in questions
        }


class FakeStore:
    def __init__(self, events=None):
        self.events = list(events or [])

    def list_events(self, task_id=None, limit=None, **filters):
        return [e for e in self.events if e.get("task_id") == task_id]

    def record_event(self, event_type, payload, workflow_id=None, node_id=None,
                     task_id=None, agent_id=None, source="system", timestamp=None):
        self.events.append({
            "event_type": event_type, "payload": payload, "task_id": task_id,
            "workflow_id": workflow_id, "node_id": node_id, "source": source,
            "timestamp": timestamp or time.time(),
        })


def _task(**overrides):
    task = {
        "task_id": "t-1",
        "workflow_id": "w-1",
        "node": "implementation",
        "status": "agent_done",
        "goal": "Implement semantic supervisor v1",
        "agent": "claude",
        "runtime": {"status": "running", "agent": "claude",
                    "agent_name": "proj-1-impl", "started_at": time.time() - 900},
    }
    task.update(overrides)
    return task


class SupervisorStateTests(unittest.TestCase):
    def test_state_is_bounded(self):
        task = _task(goal="x" * 100000)
        state = build_supervisor_state(task, now=time.time(), max_context_size=2000)
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False)), 2000)
        self.assertIn("task_id", state)

    def test_state_redacts_secrets_and_drops_raw_logs(self):
        task = _task(goal="use api_key= sk-abcdefghijklmnop and password=hunter2 for deploy")
        state = build_supervisor_state(
            task, now=time.time(),
            events=[{"event_type": "task_transitioned", "timestamp": time.time(),
                     "payload": {"reason": "HERDR done", "raw_stdout": "A" * 5000}}],
            facts={"output_summary": "Bearer abcdefghijklmnop token leaked here"},
        )
        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("sk-abcdefghijklmnop", blob)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("abcdefghijklmnop", redact_text(state["recent_output_summary"]))
        self.assertNotIn("raw_stdout", blob, "event payloads must not ride along")
        for row in state["recent_events"]:
            self.assertEqual({"type", "ago_seconds", "source", "note"} >= set(row), True)

    def test_previous_signals_included_for_trend_context(self):
        previous = {"signals": {"worker_stuck": 0.1}}
        state = build_supervisor_state(_task(), now=time.time(), previous_evaluation=previous)
        self.assertEqual(state["previous_signals"], {"worker_stuck": 0.1})


class SignalDefinitionTests(unittest.TestCase):
    def test_v1_defines_exactly_nine_signals(self):
        self.assertEqual(len(SIGNAL_NAMES), 9)
        questions = signal_questions()
        self.assertEqual(set(questions), set(SIGNAL_NAMES))
        for question in questions.values():
            self.assertEqual(question["type"], "noul")
            self.assertIn("true", question["criteria"] and question["criteria"])

    def test_signal_flags_can_disable(self):
        questions = signal_questions({"worker_stuck": False})
        self.assertNotIn("worker_stuck", questions)


class SupervisorEvaluationTests(unittest.TestCase):
    def _supervisor(self, provider=None, **cfg_over):
        config = load_config(path="/nonexistent-supervisor.json")
        config.update(cfg_over)
        return SemanticSupervisor(config, provider or StubProvider()), provider or StubProvider()

    def test_batched_evaluation_produces_all_signals(self):
        provider = StubProvider()
        supervisor, provider = self._supervisor(provider)
        evaluation = supervisor.evaluate(_task(), "agent_done", now=1000.0)
        self.assertEqual(provider.calls, 1, "one checkpoint = one provider request")
        self.assertEqual(evaluation["status"], "ok")
        self.assertEqual(set(evaluation["signals"]), set(SIGNAL_NAMES))
        self.assertEqual(evaluation["trigger"], "agent_done")
        self.assertEqual(evaluation["latency_ms"], 42.0)

    def test_previous_evaluation_delta_and_trend(self):
        previous = {
            "evaluation_id": "prev-1",
            "timestamp": 900.0,
            "signals": {"worker_stuck": 0.11, "requirements_satisfied": 0.41},
        }
        provider = StubProvider(signals=dict(ALL_SIGNALS, worker_stuck=0.64,
                                             requirements_satisfied=0.63))
        supervisor, _ = self._supervisor(provider)
        evaluation = supervisor.evaluate(_task(), "agent_done",
                                         previous_evaluation=previous, now=1000.0)
        self.assertEqual(evaluation["previous_evaluation_id"], "prev-1")
        self.assertAlmostEqual(evaluation["deltas"]["worker_stuck"]["delta"], 0.53)
        self.assertAlmostEqual(evaluation["deltas"]["requirements_satisfied"]["delta"], 0.22)
        trends = evaluation_deltas(evaluation)
        self.assertEqual(trends["worker_stuck"]["trend"], "rising")

    def test_min_interval_blocks_second_call(self):
        provider = StubProvider()
        supervisor, provider = self._supervisor(provider)
        first = supervisor.evaluate(_task(), "agent_done", now=1000.0)
        second = supervisor.evaluate(_task(), "tests_completed", now=1010.0)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "every event must not call the provider")
        self.assertEqual(provider.calls, 1)
        later = supervisor.evaluate(_task(), "tests_completed", now=1000.0 + DEFAULTS["interval"] + 1)
        self.assertIsNotNone(later)
        self.assertEqual(provider.calls, 2)

    def test_max_calls_budget_stops_runaway_supervision(self):
        provider = StubProvider()
        supervisor, provider = self._supervisor(provider, max_calls_per_task=2, interval=0, cooldown=0)
        for i in range(5):
            supervisor.evaluate(_task(), f"trigger_{i}", now=1000.0 + i)
        self.assertEqual(provider.calls, 2)

    def test_repeated_same_trigger_within_cooldown_aggregates(self):
        gate = RateGate()
        config = dict(DEFAULTS, interval=0, cooldown=300, max_calls_per_task=10)
        now = time.time()
        self.assertIsNone(gate.check("t", "agent_done", config, now=now))
        gate.record("t", "agent_done", now=now)
        self.assertIsNotNone(gate.check("t", "agent_done", config, now=now + 1))
        # after interval passes (interval=0), a *different* trigger may proceed
        self.assertIsNone(gate.check("t", "tests_completed", config, now=now + 1))

    def test_out_of_range_provider_values_are_clamped(self):
        provider = StubProvider(signals=dict(ALL_SIGNALS, worker_stuck=1.7,
                                             work_off_track=-0.4))
        supervisor, _ = self._supervisor(provider)
        evaluation = supervisor.evaluate(_task(), "agent_done", now=1000.0)
        self.assertEqual(evaluation["signals"]["worker_stuck"], 1.0)
        self.assertEqual(evaluation["signals"]["work_off_track"], 0.0)

    def test_evaluation_error_is_redacted(self):
        from herdr.supervisor.evaluation import redact_free_error

        cleaned = redact_free_error("request failed api_key=sk-abcdefghijklmnop")
        self.assertNotIn("sk-abcdefghijklmnop", cleaned)
        self.assertIn("redacted", cleaned)


class FailSafeProviderTests(unittest.TestCase):
    def test_provider_failure_yields_failed_evaluation_not_exception(self):
        from herdr.decision.models import DecisionProviderError
        provider = StubProvider(raise_error=DecisionProviderError("jev down", kind="unavailable"))
        config = load_config(path="/nonexistent-supervisor.json")
        supervisor = SemanticSupervisor(config, provider)
        evaluation = supervisor.evaluate(_task(), "agent_done", now=1000.0)
        self.assertEqual(evaluation["status"], "failed")
        self.assertIn("jev down", evaluation["error"])

    def test_unexpected_provider_crash_is_contained(self):
        provider = StubProvider(raise_error=RuntimeError("sdk exploded"))
        config = load_config(path="/nonexistent-supervisor.json")
        supervisor = SemanticSupervisor(config, provider)
        evaluation = supervisor.evaluate(_task(), "agent_done", now=1000.0)
        self.assertEqual(evaluation["status"], "failed")


class HarnessPersistenceTests(unittest.TestCase):
    def test_checkpoint_records_evaluation_and_policy_events(self):
        store = FakeStore()
        provider = StubProvider()
        config = load_config(path="/nonexistent-supervisor.json")
        result = run_checkpoint(
            task=_task(), trigger="agent_done", store=store,
            config=config, supervisor=SemanticSupervisor(config, provider),
            log=lambda _msg: None,
        )
        self.assertIsNotNone(result)
        types = [e["event_type"] for e in store.events]
        self.assertEqual(types, [EVALUATION_EVENT, "supervisor_policy"])
        decision = store.events[1]["payload"]
        self.assertEqual(decision["action"], "VERIFY")
        self.assertTrue(decision["reasons"])
        self.assertEqual(store.events[0]["source"], "semantic_supervisor")

    def test_second_checkpoint_sees_previous_evaluation(self):
        store = FakeStore()
        config = load_config(path="/nonexistent-supervisor.json")
        config["interval"] = 0  # allow back-to-back evaluations in-test
        provider = StubProvider()
        supervisor = SemanticSupervisor(config, provider)
        run_checkpoint(task=_task(), trigger="agent_done", store=store,
                       config=config, supervisor=supervisor, log=lambda _m: None)
        provider.signals["worker_stuck"] = 0.9
        later = run_checkpoint(
            task=_task(), trigger="tests_completed", store=store,
            config=config, supervisor=supervisor, log=lambda _m: None,
        )
        self.assertIsNotNone(later)
        events = [e for e in store.events if e["event_type"] == EVALUATION_EVENT]
        self.assertEqual(len(events), 2)
        self.assertEqual(later["evaluation"]["previous_evaluation_id"],
                         events[0]["payload"]["evaluation_id"])
        self.assertIsNotNone(latest_evaluation(store.events))


if __name__ == "__main__":
    unittest.main()
