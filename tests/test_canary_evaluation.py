"""Adaptive Router Canary Evaluation tests.

Contract under test:
- herdr/canary_evaluation.py: read-only join of frozen mode="canary"
  route_decision payloads with immutable agent_execution_outcomes, split
  into the adaptive arm (diverted=True) and the legacy arm
  (diverted=False), reporting observed facts only — no rollout verdict.
- Identity/attribution follows the single shadow contract: an outcome
  joins only when decision.actual_agent == outcome.agent.
- Determinism: identical inputs produce byte-identical reports.
"""

import hashlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from herdr import adaptive_router, canary_evaluation, eval_store
from herdr import execution_outcome
from herdr.state_store import get_state_store

BASE_TS = 1_700_000_000.0
NODE = "implementation"
TASK_TYPE = "fix"
WF_ID = "wf-canary-eval"


def _make_env(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-canary-eval-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    # Seeds settle through the explicit canonical finalizer with
    # controlled timestamps; write-path auto-finalization stays off.
    env_patch = patch.dict(
        "os.environ", {"HERDR_OUTCOME_AUTOFINALIZE": "0"})
    env_patch.start()
    test.addCleanup(env_patch.stop)
    store = get_state_store(tmp_path / "state.db")
    return store, tmp_path / "state.db"


def _settle_execution(store, db_path, idx, agent, *, success=True,
                      wall=600.0, rework=0, blocked=0, human=0,
                      node=NODE, task_type=TASK_TYPE, ts=None,
                      workflow_id=WF_ID, run_id=None, task_id=None):
    """One settled agent_execution_outcome with fully known metrics."""
    ts = BASE_TS + idx if ts is None else ts
    run_id = run_id or f"run-can-{idx}"
    task_id = task_id or f"task-can-{idx}"
    history = ["pending", "dispatched", "working"]
    history += ["blocked", "working"] * int(blocked)
    history.append("agent_done")
    history += ["rework", "working", "agent_done"] * int(rework)
    status = "completed" if success else "failed"
    history.append(status)
    store.save_task({
        "task_id": task_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "node": node,
        "stage": node,
        "task_type": task_type,
        "agent": agent,
        "status": status,
        "stage_verdict": "pass" if success else "blocked",
        "status_history": [{"to": s} for s in history],
        "started_at": ts,
        "finished_at": ts + wall,
        "created_at": ts,
    })
    eval_store.record_eval_result(
        run_id,
        requirements_satisfied=bool(success),
        verification_passed=True,
        human_intervention_count=int(human),
        final_status=status,
        task_id=task_id,
        workflow_id=workflow_id,
        created_at=ts + wall + 1.0,
        db_path=db_path,
    )
    settled = execution_outcome.finalize_execution_outcome(
        task_id, db_path=db_path, finalized_at=ts + wall + 5.0)
    assert settled["status"] == "created", settled
    return run_id, task_id


def _record_canary_decision(store, run_id, task_id, *, actual_agent,
                            recommended_agent, legacy_agent, diverted,
                            node=NODE, task_type=TASK_TYPE, ts=None,
                            etqs=600.0, with_ranking=True):
    if ts is None:
        digest = hashlib.sha256(run_id.encode("utf-8")).digest()
        ts = BASE_TS + 50_000 + (int.from_bytes(digest[:4], "big") % 1000)
    rankings = []
    if with_ranking:
        rankings = [{
            "agent": recommended_agent, "rank": 1, "sample_count": 30,
            "confidence": 0.9, "qualified_success_rate": 0.9,
            "blended_success_rate": 0.9, "etqs_seconds": float(etqs),
            "p50_wall_time_seconds": float(etqs),
        }]
    payload = adaptive_router.build_canary_decision(
        workflow_id=WF_ID, run_id=run_id, task_id=task_id,
        node=node, task_type=task_type, actual_agent=actual_agent,
        recommended_agent=recommended_agent, legacy_agent=legacy_agent,
        diverted=bool(diverted), rankings=rankings,
        gate={"bucket_key": f"{recommended_agent}/{node}/{task_type}",
              "hash_bucket": 1, "effective_percentage": 10,
              "hash_divert": bool(diverted), "would_divert": True,
              "truncated": False, "admission": {}},
        created_at=float(ts),
    )
    store.record_event(
        "route_decision", payload,
        workflow_id=WF_ID, node_id=node, task_id=task_id,
        agent_id=actual_agent, source="adaptive-router-canary",
        timestamp=float(ts), run_id=run_id,
    )
    return run_id, task_id


class CanaryEvaluationTestBase(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)


class TestCanaryArms(CanaryEvaluationTestBase):
    def _seed_two_arm_bucket(self):
        # Adaptive arm (diverted): 3 qualified successes, walls 100/200/300,
        # rework 1 on the first, frozen etqs 150.
        specs = [
            (0, "codex", True, 100.0, True, 1),
            (1, "codex", True, 200.0, True, 0),
            (2, "codex", True, 300.0, True, 0),
        ]
        for idx, agent, diverted, wall, success, rework in specs:
            run_id, task_id = _settle_execution(
                self.store, self.db_path, idx, agent,
                success=success, wall=wall, rework=rework)
            _record_canary_decision(
                self.store, run_id, task_id, actual_agent=agent,
                recommended_agent=agent, legacy_agent="opencode",
                diverted=diverted, etqs=150.0)
        # Legacy arm (not diverted): 2 executions, one success.
        for idx, agent, diverted, wall, success in [
            (10, "opencode", False, 400.0, True),
            (11, "opencode", False, 600.0, False),
        ]:
            run_id, task_id = _settle_execution(
                self.store, self.db_path, idx, agent,
                success=success, wall=wall)
            _record_canary_decision(
                self.store, run_id, task_id, actual_agent=agent,
                recommended_agent="codex", legacy_agent=agent,
                diverted=diverted, etqs=150.0)

    def test_arms_split_and_metrics_exact(self):
        self._seed_two_arm_bucket()
        bundle = canary_evaluation.run_canary_evaluation(self.db_path)
        report = bundle["report"]
        self.assertEqual(report["coverage"]["canary_decisions"], 5)
        self.assertEqual(report["coverage"]["settled_canary_executions"], 5)
        self.assertEqual(report["coverage"]["diverted_decisions"], 3)
        self.assertEqual(len(report["buckets"]), 1)
        bucket = report["buckets"][0]
        self.assertEqual(bucket["node"], NODE)
        self.assertEqual(bucket["task_type"], TASK_TYPE)
        adaptive = bucket["adaptive_arm"]
        self.assertEqual(adaptive["sample_count"], 3)
        self.assertAlmostEqual(adaptive["qualified_success_rate"], 1.0)
        self.assertEqual(adaptive["median_wall_time_seconds"], 200.0)
        self.assertAlmostEqual(adaptive["mean_rework_count"], 1 / 3, places=4)
        # Frozen etqs 150 vs walls 100/200/300 -> |50|,|50|,|150| -> 50.
        self.assertEqual(adaptive["etqs_mae_seconds"], 50.0)
        legacy = bucket["legacy_arm"]
        self.assertEqual(legacy["sample_count"], 2)
        self.assertAlmostEqual(legacy["qualified_success_rate"], 0.5)
        self.assertEqual(legacy["median_wall_time_seconds"], 500.0)
        delta = bucket["delta"]
        self.assertAlmostEqual(delta["qualified_success_rate_delta"], 0.5)
        self.assertEqual(delta["median_wall_time_delta_seconds"], -300.0)

    def test_report_is_deterministic(self):
        self._seed_two_arm_bucket()
        first = canary_evaluation.run_canary_evaluation(self.db_path)["report"]
        second = canary_evaluation.run_canary_evaluation(self.db_path)["report"]
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True))


class TestCanaryCoverage(CanaryEvaluationTestBase):
    def test_decision_without_outcome_is_coverage_only(self):
        run_id, task_id = _record_canary_decision(
            self.store, "run-open-1", "task-open-1", actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True)
        bundle = canary_evaluation.run_canary_evaluation(self.db_path)
        report = bundle["report"]
        self.assertEqual(report["coverage"]["canary_decisions"], 1)
        self.assertEqual(report["coverage"]["settled_canary_executions"], 0)
        self.assertEqual(report["coverage"]["diverted_decisions"], 1)
        self.assertEqual(report["buckets"], [])

    def test_outcome_agent_mismatch_never_joins(self):
        # Same (task_id, run_id) identity, but the outcome was produced by a
        # different agent than the decision's actual_agent: attribution
        # fails closed, the row keeps coverage only.
        run_id, task_id = _record_canary_decision(
            self.store, "run-x-1", "task-x-1", actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True)
        _settle_execution(self.store, self.db_path, 99, "opencode",
                          run_id=run_id, task_id=task_id)
        outcome = execution_outcome.get_execution_outcome(
            task_id, run_id, db_path=self.db_path)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["agent"], "opencode")
        rows = canary_evaluation.collect_canary_rows(self.db_path)[0]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])

    def test_shadow_mode_events_never_enter(self):
        run_id, task_id = _settle_execution(
            self.store, self.db_path, 0, "codex", wall=100.0)
        payload = adaptive_router.build_shadow_decision(
            workflow_id=WF_ID, run_id=run_id, task_id=task_id,
            node=NODE, task_type=TASK_TYPE, actual_agent="codex",
            rankings=[], created_at=BASE_TS + 99_000,
        )
        self.store.record_event(
            "route_decision", payload,
            workflow_id=WF_ID, node_id=NODE, task_id=task_id,
            agent_id="codex", source="adaptive-router-shadow",
            timestamp=BASE_TS + 99_000, run_id=run_id,
        )
        rows = canary_evaluation.collect_canary_rows(self.db_path)[0]
        self.assertEqual(rows, [])

    def test_node_filter_excludes_other_buckets(self):
        run_id, task_id = _settle_execution(
            self.store, self.db_path, 0, "codex", wall=100.0)
        _record_canary_decision(
            self.store, run_id, task_id, actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True)
        run2, task2 = _settle_execution(
            self.store, self.db_path, 1, "claude", wall=100.0,
            node="test")
        _record_canary_decision(
            self.store, run2, task2, actual_agent="claude",
            recommended_agent="claude", legacy_agent="opencode",
            diverted=True, node="test")
        rows, _meta = canary_evaluation.collect_canary_rows(
            self.db_path, node=NODE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node"], NODE)


class TestCanaryRender(CanaryEvaluationTestBase):
    def test_render_reports_both_arms_and_facts_note(self):
        run_id, task_id = _settle_execution(
            self.store, self.db_path, 0, "codex", wall=100.0)
        _record_canary_decision(
            self.store, run_id, task_id, actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True, etqs=120.0)
        bundle = canary_evaluation.run_canary_evaluation(self.db_path)
        text = canary_evaluation.render_canary_report(bundle["report"])
        self.assertIn("adaptive", text)
        self.assertIn("legacy", text)
        self.assertIn("#104", text)  # no-rollout-verdict note is visible


class TestCanaryEvalCLI(CanaryEvaluationTestBase):
    @staticmethod
    def _load_module(name):
        import importlib.machinery
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_loader(
            name,
            importlib.machinery.SourceFileLoader(
                name, str(root / "bin" / "herdr-task")),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_canary_eval_json(self):
        run_id, task_id = _settle_execution(
            self.store, self.db_path, 0, "codex", wall=100.0)
        _record_canary_decision(
            self.store, run_id, task_id, actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True)
        module = self._load_module("herdr-task-canary-eval")
        args = type("Args", (), {})()
        args.node = None
        args.task_type = None
        args.since = None
        args.limit = "1000"
        args.json = True
        buffer = StringIO()
        with patch.dict("os.environ", {"HERDR_STATE_DB": str(self.db_path)}):
            with redirect_stdout(buffer):
                module.cmd_canary_eval(args)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["coverage"]["canary_decisions"], 1)
        self.assertEqual(report["buckets"][0]["adaptive_arm"]["sample_count"],
                         1)


if __name__ == "__main__":
    unittest.main()
