"""Policy Engine tests: every action fires only from signals + deterministic
facts together, confidence policy caps high-risk moves, and provider signals
can never bypass HAFlow state facts."""

import unittest

from herdr.supervisor import policy
from herdr.supervisor.config import load_config

CONFIG = load_config(path="/nonexistent-supervisor.json")


def _evaluation(signals, certainty=None, status="ok"):
    if certainty is None and signals:
        certainty = min(abs(2.0 * v - 1.0) for v in signals.values())
    return {"evaluation_id": "e1", "status": status, "signals": dict(signals),
            "certainty": certainty, "trigger": "agent_done"}


def _facts(**overrides):
    facts = {"task_status": "working", "runtime_status": "running",
             "attempt_count": 0, "verification_count": 0,
             "auto_execute_allowed": True}
    facts.update(overrides)
    return facts


class ContinueTests(unittest.TestCase):
    def test_quiet_progress_continues(self):
        decision = policy.decide(_evaluation({
            "meaningful_progress": 0.8, "worker_stuck": 0.1, "work_off_track": 0.1,
            "requirements_satisfied": 0.4, "implementation_complete": 0.4,
            "tests_sufficient": 0.7, "needs_verification": 0.2,
            "needs_human": 0.1, "ready_to_finish": 0.2,
        }), _facts(), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)
        self.assertTrue(decision.reasons)


class VerifyTests(unittest.TestCase):
    def test_v1_success_scenario_verifies(self):
        # requirements satisfied but tests insufficient + needs_verification high
        decision = policy.decide(_evaluation({
            "meaningful_progress": 0.87, "worker_stuck": 0.08, "work_off_track": 0.14,
            "requirements_satisfied": 0.76, "implementation_complete": 0.71,
            "tests_sufficient": 0.58, "needs_verification": 0.82,
            "needs_human": 0.06, "ready_to_finish": 0.49,
        }), _facts(task_status="agent_done"), CONFIG)
        self.assertEqual(decision.action, policy.VERIFY)
        joined = " ".join(decision.reasons)
        self.assertIn("needs_verification", joined)
        self.assertIn("tests_sufficient", joined)

    def test_verify_is_allowed_even_at_low_certainty(self):
        decision = policy.decide(_evaluation({
            "requirements_satisfied": 0.71, "implementation_complete": 0.71,
            "tests_sufficient": 0.52, "needs_verification": 0.55,
        }, certainty=0.03), _facts(), CONFIG)
        self.assertEqual(decision.action, policy.VERIFY)


class RetryTests(unittest.TestCase):
    STUCK = {"worker_stuck": 0.9, "meaningful_progress": 0.05}

    def test_stuck_running_with_budget_retries(self):
        decision = policy.decide(_evaluation(self.STUCK), _facts(), CONFIG)
        self.assertEqual(decision.action, policy.RETRY)

    def test_stuck_without_live_runtime_does_not_retry(self):
        # Deterministic facts veto: only the runtime layer knows liveness.
        decision = policy.decide(_evaluation(self.STUCK),
                                 _facts(runtime_status="unavailable"), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)

    def test_stuck_with_exhausted_attempts_does_not_retry(self):
        decision = policy.decide(_evaluation(self.STUCK),
                                 _facts(attempt_count=CONFIG["policy"]["max_attempts"]),
                                 CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)

    def test_settled_task_ignores_signals_entirely(self):
        decision = policy.decide(_evaluation({"ready_to_finish": 0.99}),
                                 _facts(task_status="completed"), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)

    def test_low_certainty_degrades_retry_to_continue(self):
        decision = policy.decide(
            _evaluation({"worker_stuck": 0.72, "meaningful_progress": 0.51}, certainty=0.02),
            _facts(), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)
        self.assertIn("confidence policy", " ".join(decision.reasons))


class EscalateTests(unittest.TestCase):
    HUMAN = {"needs_human": 0.9}

    def test_needs_human_with_exhausted_retries_escalates(self):
        decision = policy.decide(_evaluation(self.HUMAN),
                                 _facts(attempt_count=3), CONFIG)
        self.assertEqual(decision.action, policy.ESCALATE)

    def test_needs_human_with_budget_remaining_continues(self):
        decision = policy.decide(_evaluation(self.HUMAN), _facts(attempt_count=0), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)

    def test_non_autonomous_task_escalates_immediately(self):
        decision = policy.decide(_evaluation(self.HUMAN),
                                 _facts(auto_execute_allowed=False), CONFIG)
        self.assertEqual(decision.action, policy.ESCALATE)

    def test_low_certainty_blocks_escalate(self):
        decision = policy.decide(
            _evaluation({"needs_human": 0.62}, certainty=0.04),
            _facts(attempt_count=5), CONFIG)
        self.assertEqual(decision.action, policy.CONTINUE)
        self.assertIn("confidence policy", " ".join(decision.reasons))


class FinishAndOffTrackTests(unittest.TestCase):
    def test_all_gates_pass_finishes(self):
        decision = policy.decide(_evaluation({
            "ready_to_finish": 0.9, "requirements_satisfied": 0.9, "tests_sufficient": 0.9,
        }), _facts(), CONFIG)
        self.assertEqual(decision.action, policy.FINISH)

    def test_off_track_pauses_when_reroute_disabled(self):
        decision = policy.decide(_evaluation({"work_off_track": 0.9}), _facts(), CONFIG)
        self.assertEqual(decision.action, policy.PAUSE)

    def test_off_track_reroutes_only_when_policy_enables(self):
        config = load_config(path="/nonexistent-supervisor.json")
        config["policy"]["allow_reroute"] = True
        decision = policy.decide(_evaluation({"work_off_track": 0.9}), _facts(), config)
        self.assertEqual(decision.action, policy.REROUTE)

    def test_off_track_exhausted_escalates(self):
        decision = policy.decide(_evaluation({"work_off_track": 0.95}),
                                 _facts(attempt_count=5), CONFIG)
        self.assertEqual(decision.action, policy.ESCALATE)


class DecisionRecordTests(unittest.TestCase):
    def test_decision_carries_explainable_record(self):
        decision = policy.decide(_evaluation({"worker_stuck": 0.9}), _facts(), CONFIG)
        record = decision.to_dict()
        self.assertEqual(record["action"], "RETRY")
        self.assertEqual(record["signals_used"]["worker_stuck"], 0.9)
        self.assertEqual(record["facts_used"]["runtime_status"], "running")
        self.assertIsNone(record["suggested_execution"], "V1 reserves but never fills")
        self.assertIsNone(record["suggested_model_tier"])


if __name__ == "__main__":
    unittest.main()
