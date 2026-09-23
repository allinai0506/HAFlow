"""Semantic Supervisor V1.1 Tests: tests_completed Continuous Evaluation Checkpoint.

Comprehensive verification of Scenarios A through J:
A. New test result triggers evaluation
B. Identical test result does not repeat evaluation
C. New test result triggers evaluation again with delta
D. Controller restart does not repeat old results (persisted dedup)
E. RateGate deferral preserves latest un-evaluated evidence
F. tests_completed cannot finish or complete a task
G. Intermediate test failures do not interrupt a working agent
H. Consecutive un-improving failures allow policy intervention
I. Kill switches result in zero provider calls and zero evidence I/O
J. Bounded, redacted evidence safety (no full logs, secrets, or source dumps)
"""

import importlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.decision.base import DecisionProvider
from herdr.decision.models import DecisionResult
from herdr.evaluator import LOOP_DIR_NAME, init_loop, write_state
from herdr.observation import ObservationStore, list_observations
from herdr.state_store import SQLiteStateStore
from herdr.supervisor import evidence as supervisor_evidence
from herdr.supervisor import harness as supervisor_harness
from herdr.supervisor import policy as policy_engine
from herdr.supervisor.config import load_config
from herdr.supervisor.evaluation import (
    EVALUATION_EVENT,
    latest_tests_completed_evidence_id,
)
from herdr.supervisor.state import build_supervisor_state
from herdr.trajectory import TrajectoryLedger


class _MockProvider(DecisionProvider):
    name = "mock_provider"

    def __init__(self, signals=None):
        self.call_count = 0
        self.last_state = None
        self.signals = signals or {
            "meaningful_progress": 0.8,
            "worker_stuck": 0.1,
            "work_off_track": 0.1,
            "requirements_satisfied": 0.7,
            "implementation_complete": 0.7,
            "tests_sufficient": 0.8,
            "needs_verification": 0.2,
            "needs_human": 0.1,
            "ready_to_finish": 0.6,
        }

    def available(self) -> bool:
        return True

    def judge(self, question, state):
        return DecisionResult(value=0.5, confidence=0.9)

    def score(self, question, state, levels):
        return DecisionResult(value=0.5, confidence=0.9)

    def choose(self, question, state, options):
        return DecisionResult(value=0.5, confidence=0.9)

    def judge_many(self, questions, context):
        self.call_count += 1
        self.last_state = dict(context)
        return {
            q: DecisionResult(
                value=self.signals.get(q, 0.5),
                confidence=0.95,
            )
            for q in questions
        }


class TestsCompletedCheckpointSuite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="supervisor-v11-test-")
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.clone_dir = self.tmp_dir / "clone_task_1"
        self.clone_dir.mkdir(parents=True, exist_ok=True)
        (self.clone_dir / ".git").mkdir()

        # Load controller module
        self.controller = importlib.import_module("services.herdr-controller")
        supervisor_harness.reset_process_state()

        self.base_config = {
            "enabled": True,
            "provider": "jev",
            "enforce": True,
            "interval": 300,
            "cooldown": 120,
            "max_calls_per_task": 10,
            "jev": {"enabled": True, "api_key_env": "TEST_KEY"},
        }
        os.environ["TEST_KEY"] = "sk-fake-valid-key"

    def tearDown(self):
        supervisor_harness.reset_process_state()
        os.environ.pop("TEST_KEY", None)
        self.tmp.cleanup()

    def _write_loop(self, iteration, total, passed, failing, score=100.0, converged=True):
        loop_dir = self.clone_dir / LOOP_DIR_NAME
        loop_dir.mkdir(parents=True, exist_ok=True)
        write_state(loop_dir, iteration=iteration, max_iter=5, status="converged" if converged else "iterating", converged=converged)
        metrics = {
            "iteration": iteration,
            "total_tests": total,
            "passed_tests": passed,
            "failing_tests": failing,
            "lint_errors": 0,
            "type_errors": 0,
            "composite_score": score,
            "has_repro_test": False,
        }
        (loop_dir / "METRICS.json").write_text(json.dumps(metrics), encoding="utf-8")
        # Write EVAL_DONE.json as a FULL snapshot (mirrors bin/herdr-loop write order).
        # Supervisor reads ONLY from EVAL_DONE.json; METRICS.json is for herdr-loop display.
        import time as _time
        (loop_dir / "EVAL_DONE.json").write_text(json.dumps({
            "iteration": iteration,
            "completed_at": _time.time(),
            "status": "converged" if converged else "iterating",
            "converged": converged,
            "max_iterations": 5,
            "total_tests": total,
            "passed_tests": passed,
            "failing_tests": list(failing),
            "lint_errors": 0,
            "type_errors": 0,
            "composite_score": score,
            "has_repro_test": False,
        }), encoding="utf-8")

    # --- A. 新测试结果触发 ---
    def test_scenario_a_new_test_result_triggers_evaluation(self):
        self._write_loop(iteration=1, total=10, passed=10, failing=[], score=100.0, converged=True)
        task = {
            "task_id": "task-a",
            "workflow_id": "wf-a",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            res = self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)

        self.assertIsNotNone(res)
        self.assertEqual(provider.call_count, 1)

        # Check that tests_completed event was recorded
        events = self.store.list_events(task_id="task-a")
        test_events = [e for e in events if e.get("event_type") == "tests_completed"]
        self.assertEqual(len(test_events), 1)
        self.assertEqual(test_events[0]["payload"]["total_tests"], 10)
        self.assertEqual(test_events[0]["payload"]["passed_tests"], 10)

        trajectory_events = TrajectoryLedger(self.db_path).list_events("run_task-a")
        self.assertEqual([event["event_type"] for event in trajectory_events], [
            "observation_created", "verification_completed",
        ])
        verification_event = trajectory_events[1]
        self.assertEqual(verification_event["verification"]["type"], "tests_completed")
        self.assertTrue(verification_event["verification"]["passed"])
        observation_id = verification_event["verification"].get("observation_id")
        self.assertTrue(observation_id)
        observations = list_observations(
            run_id="run_task-a", source_type="verification",
            store=ObservationStore(self.db_path),
        )
        self.assertEqual([item.observation_id for item in observations], [observation_id])

        # Check that evaluation was recorded with evidence_id in metadata
        eval_events = [e for e in events if e.get("event_type") == EVALUATION_EVENT]
        self.assertEqual(len(eval_events), 1)
        meta = eval_events[0]["payload"].get("metadata") or {}
        self.assertIn("evidence_id", meta)
        self.assertEqual(eval_events[0]["payload"]["trigger"], "tests_completed")

    def test_trajectory_verification_passed_follows_evaluator_converged(self):
        """Zero counters do not override an evaluator result of converged=false."""
        self._write_loop(iteration=1, total=10, passed=10, failing=[], score=100.0, converged=False)
        task = {
            "task_id": "task-converged-false",
            "workflow_id": "wf-converged-false",
            "run_id": "run-converged-false",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)

        trajectory_events = TrajectoryLedger(self.db_path).list_events("run-converged-false")
        verification = next(
            event["verification"]
            for event in trajectory_events
            if event["event_type"] == "verification_completed"
        )
        self.assertFalse(verification["passed"])
        self.assertEqual(verification["failing_count"], 0)
        self.assertEqual(verification["lint_errors"], 0)
        self.assertEqual(verification["type_errors"], 0)
        self.assertEqual(verification["composite_score"], 100.0)
        self.assertTrue(verification["evidence_id"])

    # --- B. 相同测试结果不重复触发 ---
    def test_scenario_b_identical_test_result_does_not_repeat(self):
        self._write_loop(iteration=1, total=10, passed=10, failing=[], score=100.0)
        task = {
            "task_id": "task-b",
            "workflow_id": "wf-b",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            # First call -> evaluates
            res1 = self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)
            self.assertIsNotNone(res1)
            self.assertEqual(provider.call_count, 1)

            # Second call with same evidence (even after time passes) -> skips dedup!
            res2 = self.controller.check_task_tests_completed(task, store=self.store, now=2000.0)
            self.assertIsNone(res2)
            self.assertEqual(provider.call_count, 1)

    # --- C. 新测试结果再次触发 ---
    def test_scenario_c_new_test_result_triggers_again(self):
        task = {
            "task_id": "task-c",
            "workflow_id": "wf-c",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            # Iteration 1
            self._write_loop(iteration=1, total=20, passed=15, failing=["test_a", "test_b"], score=75.0, converged=False)
            self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)
            self.assertEqual(provider.call_count, 1)

            # Iteration 2 (new test evidence with progress)
            self._write_loop(iteration=2, total=20, passed=20, failing=[], score=100.0, converged=True)
            res2 = self.controller.check_task_tests_completed(task, store=self.store, now=1400.0)
            self.assertIsNotNone(res2)
            self.assertEqual(provider.call_count, 2)

            # Verify test_progress facts computed in state
            last_state = provider.last_state
            self.assertIn("test_progress", last_state)
            progress = last_state["test_progress"]
            self.assertEqual(progress["previous_passed"], 15)
            self.assertEqual(progress["current_passed"], 20)
            self.assertEqual(progress["passed_delta"], 5)
            self.assertEqual(progress["failed_delta"], -2)
            self.assertEqual(progress["score_delta"], 25.0)

    # --- D. Controller 重启后仍不会重复旧结果 ---
    def test_scenario_d_controller_restart_safe_dedup(self):
        self._write_loop(iteration=1, total=5, passed=5, failing=[], score=100.0)
        task = {
            "task_id": "task-d",
            "workflow_id": "wf-d",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        supervisor1 = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor1), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)
            self.assertEqual(provider.call_count, 1)

        # Simulate Controller process restart:
        supervisor_harness.reset_process_state()
        new_store = SQLiteStateStore(self.db_path)
        supervisor2 = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor2), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=new_store):
            res = self.controller.check_task_tests_completed(task, store=new_store, now=2000.0)
            self.assertIsNone(res)
            # Call count must stay 1; restart did not re-evaluate old result!
            self.assertEqual(provider.call_count, 1)

    # --- E. RateGate 不等于去重 (节流保留最新结果) ---
    def test_scenario_e_rategate_deferral_preserves_latest_evidence(self):
        task = {
            "task_id": "task-e",
            "workflow_id": "wf-e",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        # RateGate interval = 300s
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            # Run 1 at t=1000
            self._write_loop(iteration=1, total=10, passed=5, failing=["f1"], score=50.0)
            self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)
            self.assertEqual(provider.call_count, 1)

            # At t=1050 (50s later < 300s), new tests complete: iteration 2, score 80
            self._write_loop(iteration=2, total=10, passed=8, failing=["f2"], score=80.0)
            res_deferred = self.controller.check_task_tests_completed(task, store=self.store, now=1050.0)
            # RateGate skips
            self.assertIsNone(res_deferred)
            self.assertEqual(provider.call_count, 1)

            # At t=1350 (350s later > 300s), interval passes without new test run
            res_evaluated = self.controller.check_task_tests_completed(task, store=self.store, now=1350.0)
            self.assertIsNotNone(res_evaluated)
            # Evaluated iteration 2!
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(provider.last_state["tests"]["iteration"], 2)

    # --- F. tests_completed 不会直接完成 Task ---
    def test_scenario_f_tests_completed_does_not_finish_task(self):
        evaluation = {
            "evaluation_id": "ev-f",
            "status": "ok",
            "trigger": "tests_completed",
            "signals": {
                "ready_to_finish": 0.99,
                "tests_sufficient": 0.99,
                "requirements_satisfied": 0.99,
                "meaningful_progress": 0.95,
            },
        }
        facts = {
            "task_status": "working",
            "runtime_status": "running",
            "attempt_count": 0,
            "trigger": "tests_completed",
        }
        config = {"policy": {"allow_auto_execute": True}, "thresholds": {"ready_to_finish": 0.5, "requirements_satisfied": 0.5, "tests_sufficient": 0.5}}

        decision = policy_engine.decide(evaluation, facts, config)
        # Must be CONTINUE, NOT FINISH
        self.assertEqual(decision.action, policy_engine.CONTINUE)
        self.assertIn("tests level satisfied, agent continues", decision.reasons[0])

    # --- G. 中间测试失败不会粗暴打断 Agent ---
    def test_scenario_g_intermediate_test_failure_does_not_break_working_agent(self):
        # Worker stuck signal is high, but agent is running & working, tests improved from 10 to 3 failing
        evaluation = {
            "evaluation_id": "ev-g",
            "status": "ok",
            "trigger": "tests_completed",
            "signals": {
                "worker_stuck": 0.85,
                "meaningful_progress": 0.75,
            },
        }
        facts = {
            "task_status": "working",
            "runtime_status": "running",
            "attempt_count": 0,
            "trigger": "tests_completed",
            "test_progress": {
                "previous_failed": 10,
                "current_failed": 3,
                "failed_delta": -7,  # Improving!
            },
            "tests": {
                "iteration": 2,
                "max_iterations": 5,
                "converged": False,
            },
        }
        config = {"policy": {"max_attempts": 2}, "thresholds": {"worker_stuck": 0.5, "meaningful_progress": 0.5}}

        decision = policy_engine.decide(evaluation, facts, config)
        # Agent is making progress on tests: MUST CONTINUE, NOT RETRY
        self.assertEqual(decision.action, policy_engine.CONTINUE)
        self.assertIn("inner loop continues", decision.reasons[0])

    # --- H. 连续无改善才允许干预 ---
    def test_scenario_h_consecutive_unimproving_stuck_worker_triggers_intervention(self):
        evaluation = {
            "evaluation_id": "ev-h",
            "status": "ok",
            "trigger": "tests_completed",
            "signals": {
                "worker_stuck": 0.92,
                "meaningful_progress": 0.05,
            },
        }
        facts = {
            "task_status": "working",
            "runtime_status": "running",
            "attempt_count": 0,
            "trigger": "tests_completed",
            "test_progress": {
                "previous_failed": 20,
                "current_failed": 20,
                "failed_delta": 0,  # Zero improvement
                "score_delta": 0.0,
            },
            "tests": {
                "iteration": 5,  # Max iterations exhausted
                "max_iterations": 5,
                "converged": False,
            },
        }
        config = {"policy": {"max_attempts": 2}, "thresholds": {"worker_stuck": 0.5, "meaningful_progress": 0.5}}

        decision = policy_engine.decide(evaluation, facts, config)
        # Strict conditions met: RETRY triggered
        self.assertEqual(decision.action, policy_engine.RETRY)
        self.assertIn("consecutive non-improving tests without progress", decision.reasons[1])

    # --- I. Kill Switch ---
    def test_scenario_i_kill_switch_zero_jev_traffic(self):
        self._write_loop(iteration=1, total=5, passed=5, failing=[], score=100.0)
        task = {
            "task_id": "task-i",
            "workflow_id": "wf-i",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)

        provider = _MockProvider()
        disabled_config = dict(self.base_config)
        disabled_config["enabled"] = False  # Kill switch

        with patch.object(supervisor_harness, "load_config", return_value=disabled_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            res = self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)
            self.assertIsNone(res)
            self.assertEqual(provider.call_count, 0)

    # --- J. Evidence 安全 ---
    def test_scenario_j_evidence_safety_no_full_logs_or_keys(self):
        # Create a giant log file and leak fake credentials in test names/environment
        huge_log = "TEST FAILURE LINE " * 2000  # ~36KB
        (self.clone_dir / "test.log").write_text(huge_log, encoding="utf-8")

        test_evidence = {
            "iteration": 1,
            "total_tests": 10,
            "passed_tests": 9,
            "failing_tests": ["test_auth_sk-1234567890abcdef_secret"],
            "composite_score": 90.0,
        }
        task = {
            "task_id": "task-j",
            "workflow_id": "wf-j",
            "goal": "Implement secure auth sk-live-998877665544",
            "clone_path": str(self.clone_dir),
        }
        facts = {
            "tests": test_evidence,
            "trigger": "tests_completed",
        }

        state = build_supervisor_state(task, now=1000.0, facts=facts, max_context_size=8000)

        # 1. Credentials are redacted
        state_str = json.dumps(state)
        self.assertNotIn("sk-1234567890abcdef", state_str)
        self.assertNotIn("sk-live-998877665544", state_str)

        # 2. Huge raw log text is not included
        self.assertNotIn("TEST FAILURE LINE", state_str)

        # 3. Context size is bounded within budget
        self.assertLessEqual(len(state_str), 8000)



if __name__ == "__main__":
    unittest.main()


# ─────────────────────────────────────────────────────────────────────────────
# Regression tests for PR #61 bug-fixes (K–O)
# ─────────────────────────────────────────────────────────────────────────────


class TestsCompletedRegressionFixes(unittest.TestCase):
    """Regression tests for the five bugs fixed in PR #61 (second round)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="supervisor-v11-regression-")
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.clone_dir = self.tmp_dir / "clone_regression"
        self.clone_dir.mkdir(parents=True, exist_ok=True)
        (self.clone_dir / ".git").mkdir()

        self.controller = importlib.import_module("services.herdr-controller")
        supervisor_harness.reset_process_state()

        self.base_config = {
            "enabled": True,
            "provider": "jev",
            "enforce": True,
            "interval": 300,
            "cooldown": 120,
            "max_calls_per_task": 10,
            "jev": {"enabled": True, "api_key_env": "TEST_KEY"},
        }
        os.environ["TEST_KEY"] = "sk-fake-valid-key"

    def tearDown(self):
        supervisor_harness.reset_process_state()
        os.environ.pop("TEST_KEY", None)
        self.tmp.cleanup()

    def _write_loop(self, iteration, total, passed, failing, score=80.0, converged=False):
        """Write loop artefacts including a FULL EVAL_DONE.json snapshot (sole Supervisor source)."""
        loop_dir = self.clone_dir / LOOP_DIR_NAME
        loop_dir.mkdir(parents=True, exist_ok=True)
        write_state(loop_dir, iteration=iteration, max_iter=5,
                    status="converged" if converged else "iterating", converged=converged)
        metrics = {
            "iteration": iteration,
            "total_tests": total,
            "passed_tests": passed,
            "failing_tests": failing,
            "lint_errors": 0,
            "type_errors": 0,
            "composite_score": score,
            "has_repro_test": False,
        }
        (loop_dir / "METRICS.json").write_text(json.dumps(metrics), encoding="utf-8")
        import time as _time
        (loop_dir / "EVAL_DONE.json").write_text(json.dumps({
            "iteration": iteration,
            "completed_at": _time.time(),
            "status": "converged" if converged else "iterating",
            "converged": converged,
            "max_iterations": 5,
            "total_tests": total,
            "passed_tests": passed,
            "failing_tests": list(failing),
            "lint_errors": 0,
            "type_errors": 0,
            "composite_score": score,
            "has_repro_test": False,
        }), encoding="utf-8")

    # --- K. 撕裂快照被 sentinel 阻止（EVAL_DONE 缺失时拒绝提取）---
    def test_k_torn_snapshot_blocked_when_sentinel_missing(self):
        """extract_test_evidence returns None when EVAL_DONE.json is absent."""
        loop_dir = self.clone_dir / LOOP_DIR_NAME
        loop_dir.mkdir(parents=True, exist_ok=True)
        # Write METRICS.json and STATE.md, but deliberately omit EVAL_DONE.json
        write_state(loop_dir, iteration=2, max_iter=5, status="iterating", converged=False)
        metrics = {
            "iteration": 2, "total_tests": 10, "passed_tests": 8,
            "failing_tests": ["test_foo"], "lint_errors": 0, "type_errors": 0,
            "composite_score": 80.0, "has_repro_test": False,
        }
        (loop_dir / "METRICS.json").write_text(json.dumps(metrics), encoding="utf-8")
        # No EVAL_DONE.json written

        evidence = supervisor_evidence.extract_test_evidence(str(self.clone_dir))
        self.assertIsNone(evidence,
            "extract_test_evidence must return None when EVAL_DONE.json is absent")

    # --- L. EVAL_DONE.json 为旧快照时，METRICS 已被覆盖仍安全（P1 race regression）---
    def test_l_eval_done_single_source_immune_to_metrics_overwrite(self):
        """P1 Race Regression: METRICS.json overwritten by next iteration but EVAL_DONE still has
        iteration-1 data → Supervisor must return iteration-1 evidence, never a mixed snapshot.

        Reproduces the exact torn-snapshot scenario:
          STATE=1, EVAL_DONE=1 (full iteration-1 snapshot)
          METRICS=2 (next iteration already started writing)

        Under the old multi-file design:
          - EVAL_DONE.iteration (1) == STATE.iteration (1) → guard PASSES
          - METRICS.json is then read → contains iteration-2 data
          - Result: mixed evidence (iter-1 state + iter-2 metrics) ← BUG

        Under the new single-file design:
          - Only EVAL_DONE.json is read
          - Returns iteration-1 data from the snapshot
          - METRICS.json overwrite is invisible → no mixed evidence ← FIXED
        """
        loop_dir = self.clone_dir / LOOP_DIR_NAME
        loop_dir.mkdir(parents=True, exist_ok=True)

        # Set up state as if iteration 1 fully completed
        write_state(loop_dir, iteration=1, max_iter=5, status="iterating", converged=False)
        import time as _time

        # EVAL_DONE.json reflects iteration 1 (correct, complete snapshot)
        (loop_dir / "EVAL_DONE.json").write_text(json.dumps({
            "iteration": 1,
            "completed_at": _time.time(),
            "status": "iterating",
            "converged": False,
            "max_iterations": 5,
            "total_tests": 10,
            "passed_tests": 8,
            "failing_tests": ["test_foo"],
            "lint_errors": 0,
            "type_errors": 0,
            "composite_score": 80.0,
            "has_repro_test": False,
        }), encoding="utf-8")

        # Simulate race: METRICS.json has already been overwritten by iteration 2
        # (run_evaluation writes METRICS.json FIRST, EVAL_DONE.json LAST)
        metrics_iter2 = {
            "iteration": 2, "total_tests": 10, "passed_tests": 10,
            "failing_tests": [], "lint_errors": 0, "type_errors": 0,
            "composite_score": 100.0, "has_repro_test": False,
        }
        (loop_dir / "METRICS.json").write_text(json.dumps(metrics_iter2), encoding="utf-8")

        # extract_test_evidence must read ONLY from EVAL_DONE.json
        evidence = supervisor_evidence.extract_test_evidence(str(self.clone_dir))

        self.assertIsNotNone(evidence, "Should return evidence from EVAL_DONE.json snapshot")
        # Must reflect iteration-1 data from EVAL_DONE, NOT iteration-2 from METRICS
        self.assertEqual(evidence["iteration"], 1,
            "iteration must come from EVAL_DONE.json (1), not the overwritten METRICS.json (2)")
        self.assertEqual(evidence["passed_tests"], 8,
            "passed_tests must come from EVAL_DONE.json iteration-1 snapshot (8), not METRICS (10)")
        self.assertEqual(evidence["composite_score"], 80.0,
            "composite_score must come from EVAL_DONE.json iteration-1 snapshot (80.0), not METRICS (100.0)")
        self.assertEqual(evidence["failing_count"], 1,
            "failing_count must reflect iteration-1 snapshot (1 failure), not iteration-2 (0)")



    # --- M. 失败的 SupervisorEvaluation 不消费 evidence_id ---
    def test_m_failed_evaluation_does_not_consume_evidence_id(self):
        """latest_tests_completed_evidence_id skips failed evaluation events."""
        from herdr.supervisor.evaluation import EVALUATION_EVENT, latest_tests_completed_evidence_id

        ev_id = "tevd-abc123"
        # Simulate a failed evaluation event (Jev timeout)
        failed_event = {
            "event_type": EVALUATION_EVENT,
            "timestamp": 1000.0,
            "payload": {
                "trigger": "tests_completed",
                "status": "failed",
                "error": "provider_timeout",
                "metadata": {"evidence_id": ev_id},
                "timestamp": 1000.0,
            },
        }
        result = latest_tests_completed_evidence_id([failed_event])
        self.assertIsNone(result,
            "A failed evaluation event must NOT consume the evidence_id; "
            "re-evaluation should be possible on the next poll")

        # But a successful evaluation with the same evidence_id IS consumed
        ok_event = {
            "event_type": EVALUATION_EVENT,
            "timestamp": 2000.0,
            "payload": {
                "trigger": "tests_completed",
                "status": "ok",
                "metadata": {"evidence_id": ev_id},
                "timestamp": 2000.0,
            },
        }
        result2 = latest_tests_completed_evidence_id([failed_event, ok_event])
        self.assertEqual(result2, ev_id,
            "A successful evaluation event must consume the evidence_id")

    # --- N. rework 中的 Agent 和 working 一样受到保护 ---
    def test_n_rework_agent_protected_same_as_working(self):
        """Policy must CONTINUE (not RETRY) when task_status=rework and iterations remain."""
        config = load_config()

        signals = {
            "meaningful_progress": 0.3,
            "worker_stuck": 0.85,   # high — would normally trigger RETRY
            "work_off_track": 0.1,
            "requirements_satisfied": 0.5,
            "implementation_complete": 0.5,
            "tests_sufficient": 0.3,
            "needs_verification": 0.2,
            "needs_human": 0.1,
            "ready_to_finish": 0.2,
        }
        facts = {
            "trigger": "tests_completed",
            "task_status": "rework",       # <-- rework, not working
            "runtime_status": "running",
            "attempt_count": 0,
            "max_attempts": 2,
            "verification_count": 0,
            "max_verifications": 2,
            "test_progress": {"failed_delta": 0, "score_delta": 0.0},
            "tests": {"iteration": 1, "max_iterations": 5, "converged": False},
        }
        decision = policy_engine.decide(signals, facts, config)
        self.assertEqual(decision.action, policy_engine.CONTINUE,
            f"Agent in rework must be CONTINUE (not RETRY) while iterations remain; "
            f"got {decision.action}: {decision.reasons}")

    # --- O. dispatched 任务不参与 tests_completed 检查 ---
    def test_o_dispatched_status_excluded_from_tests_completed_polling(self):
        """check_task_tests_completed should not be called for dispatched tasks in the polling loop."""
        # This test verifies the polling guard (status in ("working", "rework"))
        # by confirming that even if we manually call check_task_tests_completed
        # for a dispatched task with no loop artifacts, it returns None gracefully.
        task_dispatched = {
            "task_id": "task-o",
            "workflow_id": "wf-o",
            "status": "dispatched",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task_dispatched)

        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        # No loop artifacts written → extract_test_evidence returns None
        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store):
            res = self.controller.check_task_tests_completed(
                task_dispatched, store=self.store, now=1000.0
            )

        self.assertIsNone(res, "dispatched task with no loop artefacts must return None")
        self.assertEqual(provider.call_count, 0,
            "No provider calls should occur for dispatched tasks without test evidence")

    def test_p_receipt_failure_does_not_consume_evidence(self):
        """A failed verification receipt remains retryable and is not evaluated."""
        self._write_loop(iteration=1, total=3, passed=2, failing=["x"], score=66.0)
        task = {
            "task_id": "task-receipt-failure",
            "workflow_id": "wf-receipt-failure",
            "run_id": "run-receipt-failure",
            "node": "impl",
            "status": "working",
            "clone_path": str(self.clone_dir),
            "runtime": {"status": "running"},
        }
        self.store.save_task(task)
        provider = _MockProvider()
        supervisor = supervisor_harness.SemanticSupervisor(self.base_config, provider)

        with patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
             patch.object(supervisor_harness, "load_config", return_value=self.base_config), \
             patch.object(self.controller, "_get_store", return_value=self.store), \
             patch.object(self.controller, "record_trajectory_event", side_effect=RuntimeError("ledger busy")), \
             patch.object(self.controller, "supervisor_checkpoint") as checkpoint:
            result = self.controller.check_task_tests_completed(task, store=self.store, now=1000.0)

        self.assertIsNone(result)
        checkpoint.assert_not_called()
        self.assertFalse(
            [event for event in self.store.list_events(task_id=task["task_id"])
             if event.get("event_type") == EVALUATION_EVENT]
        )


if __name__ == "__main__":
    unittest.main()
