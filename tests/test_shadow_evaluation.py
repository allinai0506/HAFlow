"""Adaptive Router Shadow Evaluation v1 tests (task spec section 19, Cases 1-14).

Contract under test:
- herdr/shadow_evaluation.py: read-only retrospective evaluation over
  frozen route_decision payloads + immutable agent_execution_outcomes.
  Never recomputes rankings, never claims counterfactual wins.
- herdr/state_db.py: bounded route_decision reads + batched outcome
  point-lookups (no N+1, no unbounded loads).
- bin/herdr-task shadow-eval: read-only CLI over the same core.

Shadow is not A/B: recommended_agent never executed; uplift is always
labeled predicted/counterfactual, never observed.
"""

import json as _json
import os as _os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import adaptive_router, eval_store, execution_outcome, shadow_evaluation, state_db
from herdr.state_store import get_state_store

BASE_TS = 1_700_000_000.0
NODE = "implementation"
TASK_TYPE = "fix"


def _make_env(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-shadow-eval-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    env_patch = patch.dict(_os.environ, {"HERDR_OUTCOME_AUTOFINALIZE": "0"})
    env_patch.start()
    test.addCleanup(env_patch.stop)
    store = get_state_store(tmp_path / "state.db")
    return store, tmp_path / "state.db"


def _settle_outcome(store, db_path, task_id, run_id, agent, *,
                    success=True, wall=600.0, node=NODE, task_type=TASK_TYPE,
                    rework=False, blocked=False, human=0, ts=None,
                    workflow_id="wf-shadow"):
    """Settle one immutable outcome through the canonical finalizer."""
    ts = BASE_TS if ts is None else ts
    history = ["pending", "dispatched", "working"]
    if blocked:
        history += ["blocked", "working"]
    history.append("agent_done")
    if rework:
        history += ["rework", "working", "agent_done"]
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
        "acceptance_verdict": bool(success),
        "status_history": [{"to": s} for s in history],
        "started_at": ts,
        "finished_at": ts + wall,
        "created_at": ts,
    })
    eval_store.record_eval_result(
        run_id,
        requirements_satisfied=bool(success),
        verification_passed=bool(success),
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
    return settled["outcome"]


def _ranking_entry(agent, *, blended=0.8, qualified=None, etqs=700.0,
                   p50=600.0, samples=20, confidence=0.66, rank=1):
    return {
        "agent": agent,
        "rank": rank,
        "sample_count": samples,
        "qualified_success_count": int((qualified if qualified is not None else blended) * samples),
        "qualified_success_rate": qualified if qualified is not None else blended,
        "blended_success_rate": blended,
        "confidence": confidence,
        "p50_wall_time_seconds": p50,
        "p90_wall_time_seconds": p50 + 100.0 if p50 is not None else None,
        "rework_rate": 0.0,
        "blocked_rate": 0.0,
        "verification_failure_rate": 0.0,
        "human_intervention_rate": 0.0,
        "queue_delay_seconds": 0.0,
        "etqs_seconds": etqs,
        "candidate_index": 0,
    }


def _record_decision(store, *, task_id, run_id, workflow_id="wf-shadow",
                     node=NODE, task_type=TASK_TYPE, actual="opencode",
                     recommended="codex", actual_blended=0.72,
                     recommended_blended=0.86, decided_at=None):
    """Persist a frozen route_decision event with controlled predictions."""
    decided_at = BASE_TS + 50.0 if decided_at is None else decided_at
    if recommended == actual:
        rankings = [
            _ranking_entry(actual, blended=actual_blended,
                           etqs=980.0, rank=1),
        ]
    else:
        rankings = [
            _ranking_entry(recommended, blended=recommended_blended,
                           etqs=710.0, rank=1),
            _ranking_entry(actual, blended=actual_blended,
                           etqs=980.0, rank=2),
        ]
    decision = adaptive_router.build_shadow_decision(
        workflow_id=workflow_id, run_id=run_id, task_id=task_id,
        node=node, task_type=task_type, actual_agent=actual,
        rankings=rankings, created_at=decided_at,
    )
    assert decision["recommended_agent"] == recommended, decision
    store.record_event(
        "route_decision", decision,
        workflow_id=workflow_id or None,
        node_id=node or None,
        task_id=task_id or None,
        agent_id=actual or None,
        source="adaptive-router-shadow",
        timestamp=float(decided_at),
        run_id=run_id or None,
    )
    return decision


class CollectRowsTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_case1_matching_decision_and_outcome_builds_row(self):
        _record_decision(self.store, task_id="t-1", run_id="run-1")
        _settle_outcome(self.store, self.db_path, "t-1", "run-1", "opencode",
                        success=True, wall=760.0)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_id"], "t-1")
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(row["actual_agent"], "opencode")
        self.assertEqual(row["recommended_agent"], "codex")
        self.assertFalse(row["same_decision"])
        self.assertTrue(row["actual_outcome"]["qualified_success"])
        self.assertEqual(row["actual_outcome"]["wall_time_seconds"], 760.0)
        # Frozen predictions, not recomputed.
        self.assertAlmostEqual(
            row["actual_prediction"]["blended_success_rate"], 0.72)
        self.assertAlmostEqual(
            row["recommended_prediction"]["blended_success_rate"], 0.86)

    def test_case2_decision_without_outcome_counts_total_only(self):
        _record_decision(self.store, task_id="t-no", run_id="run-no")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        self.assertEqual(report["coverage"]["total_route_decisions"], 1)
        self.assertEqual(report["coverage"]["route_decisions_with_outcome"], 0)
        self.assertEqual(report["coverage"]["outcome_coverage_rate"], 0.0)
        # No outcome rows: actual-outcome section stays empty, not zero-filled.
        self.assertIsNone(
            report["actual_outcome"]["actual_qualified_success_rate"])

    def test_case3_same_task_id_different_run_id_never_joins(self):
        _record_decision(self.store, task_id="t-same", run_id="run-a")
        _settle_outcome(self.store, self.db_path, "t-same", "run-b",
                        "opencode", success=True)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])

    def test_case4_conflicting_workflow_id_never_joins(self):
        _record_decision(self.store, task_id="t-wf", run_id="run-wf",
                         workflow_id="wf-shadow")
        _settle_outcome(self.store, self.db_path, "t-wf", "run-wf",
                        "opencode", success=True, workflow_id="wf-other")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])

    def test_case5_same_decision_counted(self):
        _record_decision(self.store, task_id="t-s", run_id="run-s",
                         actual="codex", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-s", "run-s", "codex")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertTrue(rows[0]["same_decision"])
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        self.assertEqual(report["agreement"]["same_decision_count"], 1)
        self.assertEqual(report["agreement"]["different_decision_count"], 0)
        self.assertEqual(report["agreement"]["disagreement_rate"], 0.0)

    def test_case6_different_decision_counted(self):
        _record_decision(self.store, task_id="t-d", run_id="run-d",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-d", "run-d", "opencode")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertFalse(rows[0]["same_decision"])
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        self.assertEqual(report["agreement"]["different_decision_count"], 1)
        self.assertAlmostEqual(
            report["agreement"]["disagreement_rate"], 1.0)

    def test_case7_missing_actual_prediction_is_unavailable(self):
        store = self.store
        rankings = [_ranking_entry("codex", blended=0.9, rank=1)]
        decision = adaptive_router.build_shadow_decision(
            workflow_id="wf-shadow", run_id="run-7", task_id="t-7",
            node=NODE, task_type=TASK_TYPE, actual_agent="ghost",
            rankings=rankings, created_at=BASE_TS + 50.0)
        # actual "ghost" is not in frozen rankings: must not guess.
        self.assertEqual(decision["recommended_agent"], "codex")
        store.record_event(
            "route_decision", decision, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-7", agent_id="ghost",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-7")
        _settle_outcome(self.store, self.db_path, "t-7", "run-7", "ghost")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertIsNone(rows[0]["actual_prediction"])
        self.assertIsNotNone(rows[0]["recommended_prediction"])

    def test_case8_missing_recommended_prediction_is_unavailable(self):
        # A frozen payload whose recommended agent has no ranking entry
        # (e.g. pruned history) must surface unavailable, never guessed.
        # build_shadow_decision always embeds the recommendation, so this
        # legacy-shaped payload is recorded directly.
        payload = {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": "run-8",
            "task_id": "t-8",
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "recommended_agent": "phantom",
            "same_decision": False,
            "candidate_rankings": [
                _ranking_entry("opencode", blended=0.7, rank=1)
            ],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-8", agent_id="opencode",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-8")
        _settle_outcome(self.store, self.db_path, "t-8", "run-8", "opencode")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertIsNotNone(rows[0]["actual_prediction"])
        self.assertIsNone(rows[0]["recommended_prediction"])

    def test_case11_frozen_prediction_survives_later_outcomes(self):
        # Decision freezes blended=0.72; later outcomes change what a
        # recompute-today would say. The row must keep the frozen value.
        _record_decision(self.store, task_id="t-11", run_id="run-11",
                         actual="opencode", recommended="codex",
                         actual_blended=0.72, decided_at=BASE_TS + 50.0)
        for i in range(10):
            _settle_outcome(self.store, self.db_path, f"t-late-{i}",
                            f"run-late-{i}", "opencode", success=True,
                            ts=BASE_TS + 1000.0 + i)
        _settle_outcome(self.store, self.db_path, "t-11", "run-11",
                        "opencode", success=True, ts=BASE_TS + 2000.0)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        row = next(r for r in rows if r["task_id"] == "t-11")
        self.assertAlmostEqual(
            row["actual_prediction"]["blended_success_rate"], 0.72)
        self.assertIsNotNone(row["actual_outcome"])


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def _seed_bucket(self, prefix, blended, successes, total, start):
        for i in range(total):
            idx = start + i
            _record_decision(
                self.store, task_id=f"{prefix}-{idx}",
                run_id=f"run-{prefix}-{idx}", actual="opencode",
                recommended="opencode", actual_blended=blended,
                decided_at=BASE_TS + 50.0 + idx)
            _settle_outcome(self.store, self.db_path, f"{prefix}-{idx}",
                            f"run-{prefix}-{idx}", "opencode",
                            success=(i < successes), ts=BASE_TS + 5000.0 + idx)

    def test_case9_bucket_observed_rates(self):
        self._seed_bucket("b1", 0.5, 1, 2, 0)
        self._seed_bucket("b2", 0.7, 3, 4, 100)
        self._seed_bucket("b3", 0.9, 4, 5, 200)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        buckets = {b["bucket"]: b
                   for b in report["calibration"]["buckets"]}
        self.assertEqual(buckets["0.4-0.6"]["samples"], 2)
        self.assertAlmostEqual(
            buckets["0.4-0.6"]["observed_success_rate"], 0.5)
        self.assertEqual(buckets["0.6-0.8"]["samples"], 4)
        self.assertAlmostEqual(
            buckets["0.6-0.8"]["observed_success_rate"], 0.75)
        self.assertEqual(buckets["0.8-1.0"]["samples"], 5)
        self.assertAlmostEqual(
            buckets["0.8-1.0"]["observed_success_rate"], 0.8)

    def test_case10_brier_score_formula(self):
        # mean((0.8-1)^2, (0.2-0)^2) = 0.04
        _record_decision(self.store, task_id="t-p1", run_id="run-p1",
                         actual="opencode", recommended="opencode",
                         actual_blended=0.8)
        _settle_outcome(self.store, self.db_path, "t-p1", "run-p1",
                        "opencode", success=True)
        _record_decision(self.store, task_id="t-p2", run_id="run-p2",
                         actual="opencode", recommended="opencode",
                         actual_blended=0.2)
        _settle_outcome(self.store, self.db_path, "t-p2", "run-p2",
                        "opencode", success=False)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        self.assertAlmostEqual(
            report["calibration"]["brier_score"], 0.04, places=9)

    def test_no_counterfactual_win_rate_claim(self):
        _record_decision(self.store, task_id="t-c", run_id="run-c",
                         actual="opencode", recommended="codex",
                         actual_blended=0.2, recommended_blended=0.9)
        _settle_outcome(self.store, self.db_path, "t-c", "run-c",
                        "opencode", success=False)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        payload = _json.dumps(report)
        self.assertNotIn("win_rate", payload)
        self.assertNotIn("observed_uplift", payload)
        self.assertIn("predicted_uplift", report)
        self.assertTrue(
            report["predicted_uplift"]["note"].startswith("counterfactual"))


class GroupingAndSufficiencyTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_case12_disagreement_grouping(self):
        for i in range(3):
            _record_decision(
                self.store, task_id=f"t-g{i}", run_id=f"run-g{i}",
                actual="opencode", recommended="codex",
                decided_at=BASE_TS + 50.0 + i)
            _settle_outcome(self.store, self.db_path, f"t-g{i}",
                            f"run-g{i}", "opencode", ts=BASE_TS + 9000.0 + i)
        _record_decision(self.store, task_id="t-g9", run_id="run-g9",
                         actual="codex", recommended="claude",
                         decided_at=BASE_TS + 90.0)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        groups = {(g["node"], g["task_type"], g["actual_agent"],
                   g["recommended_agent"]): g["count"]
                  for g in report["disagreement"]["groups"]}
        self.assertEqual(
            groups.get((NODE, TASK_TYPE, "opencode", "codex")), 3)
        self.assertEqual(
            groups.get((NODE, TASK_TYPE, "codex", "claude")), 1)

    def test_case13_sufficiency_boundaries(self):
        cases = [("cold-9", 9, "cold"), ("warm-10", 10, "warming"),
                 ("warm-29", 29, "warming"), ("ok-30", 30, "sufficient")]
        for prefix, total, _ in cases:
            for i in range(total):
                _record_decision(
                    self.store, task_id=f"t-{prefix}-{i}",
                    run_id=f"run-{prefix}-{i}", actual="opencode",
                    recommended="opencode", actual_blended=0.7,
                    decided_at=BASE_TS + 50.0 + i)
                _settle_outcome(self.store, self.db_path, f"t-{prefix}-{i}",
                                f"run-{prefix}-{i}", "opencode",
                                ts=BASE_TS + 20000.0 + i)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        # Boundaries are exercised on the pure classifier directly so the
        # bucket of each synthetic agent population is unambiguous.
        self.assertEqual(
            shadow_evaluation.sufficiency_status(9), "cold")
        self.assertEqual(
            shadow_evaluation.sufficiency_status(10), "warming")
        self.assertEqual(
            shadow_evaluation.sufficiency_status(29), "warming")
        self.assertEqual(
            shadow_evaluation.sufficiency_status(30), "sufficient")
        buckets = {b["bucket_key"]: b
                   for b in report["data_sufficiency"]}
        # All rows share one actual bucket: 78 settled outcomes, all
        # carrying a frozen actual prediction.
        only = buckets[f"opencode/{NODE}/{TASK_TYPE}"]
        self.assertEqual(only["evaluation_sample_count"], 78)
        self.assertEqual(only["calibration_sample_count"], 78)
        self.assertEqual(only["evaluation_data_status"], "sufficient")

    def test_case14_json_report_deterministic(self):
        _record_decision(self.store, task_id="t-j", run_id="run-j",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-j", "run-j", "opencode")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        first = shadow_evaluation.build_shadow_evaluation_report(rows)
        rows_again = shadow_evaluation.collect_evaluation_rows(self.db_path)
        second = shadow_evaluation.build_shadow_evaluation_report(rows_again)
        self.assertEqual(
            _json.dumps(first, sort_keys=True),
            _json.dumps(second, sort_keys=True))

    def test_bounded_queries(self):
        for i in range(5):
            _record_decision(
                self.store, task_id=f"t-l{i}", run_id=f"run-l{i}",
                decided_at=BASE_TS + 50.0 + i)
        limited = state_db.query_route_decisions(limit=2, db_path=self.db_path)
        self.assertLessEqual(len(limited), 2)
        # Newest-first window keeps the latest decisions.
        self.assertEqual(limited[0]["task_id"], "t-l4")
        rows = shadow_evaluation.collect_evaluation_rows(
            self.db_path, limit=2)
        self.assertLessEqual(len(rows), 2)


class ReviewFixRegressionTest(unittest.TestCase):
    """S6 round-1 findings: uplift sample counts, unknown recommendation."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_uplift_counts_success_and_etqs_samples_separately(self):
        actual_entry = _ranking_entry("opencode", blended=0.2, rank=2)
        recommended_entry = _ranking_entry("codex", blended=0.9, rank=1)
        del recommended_entry["etqs_seconds"]
        del actual_entry["etqs_seconds"]
        payload = {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": "run-u1",
            "task_id": "t-u1",
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "recommended_agent": "codex",
            "same_decision": False,
            "candidate_rankings": [recommended_entry, actual_entry],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-u1", agent_id="opencode",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-u1")
        _settle_outcome(self.store, self.db_path, "t-u1", "run-u1",
                        "opencode", success=True)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        uplift = report["predicted_uplift"]
        self.assertEqual(uplift["n_success"], 1)
        self.assertEqual(uplift["n_etqs"], 0)
        self.assertAlmostEqual(
            uplift["median_predicted_success_uplift"], 0.7)
        self.assertIsNone(
            uplift["median_predicted_etqs_improvement_seconds"])

    def test_missing_recommended_agent_is_unknown_not_same(self):
        payload = {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": "run-u2",
            "task_id": "t-u2",
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "candidate_rankings": [
                _ranking_entry("opencode", blended=0.7, rank=1)
            ],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-u2", agent_id="opencode",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-u2")
        _settle_outcome(self.store, self.db_path, "t-u2", "run-u2",
                        "opencode", success=True)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        # Unknown recommendation must not fabricate same_decision=True.
        self.assertEqual(rows[0]["recommended_agent"], "")
        self.assertFalse(rows[0]["same_decision"])
        self.assertIsNone(rows[0]["recommended_prediction"])


class ReviewFeedbackRegressionTest(unittest.TestCase):
    """PR #101 human-review fixes: tri-state, model/eval split,
    filter-then-limit, paired ETQS."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def _unknown_payload(self, task_id, run_id):
        return {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": run_id,
            "task_id": task_id,
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "candidate_rankings": [
                _ranking_entry("opencode", blended=0.7, rank=1)
            ],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }

    def test_unknown_excluded_from_disagreement_denominator(self):
        _record_decision(self.store, task_id="t-s", run_id="run-s",
                         actual="codex", recommended="codex")
        _record_decision(self.store, task_id="t-d", run_id="run-d",
                         actual="opencode", recommended="codex")
        self.store.record_event(
            "route_decision", self._unknown_payload("t-u", "run-u"),
            workflow_id="wf-shadow", node_id=NODE, task_id="t-u",
            agent_id="opencode", source="adaptive-router-shadow",
            timestamp=BASE_TS + 50.0, run_id="run-u")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        by_task = {r["task_id"]: r for r in rows}
        self.assertEqual(by_task["t-u"]["agreement_status"], "unknown")
        self.assertEqual(by_task["t-s"]["agreement_status"], "same")
        self.assertEqual(by_task["t-d"]["agreement_status"], "different")
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        agreement = report["agreement"]
        self.assertEqual(agreement["same_decision_count"], 1)
        self.assertEqual(agreement["different_decision_count"], 1)
        self.assertEqual(agreement["unknown_decision_count"], 1)
        # different / (same + different): unknown stays out of denominator.
        self.assertAlmostEqual(agreement["disagreement_rate"], 0.5)
        groups = report["disagreement"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["actual_agent"], "opencode")
        self.assertEqual(groups[0]["recommended_agent"], "codex")

    def test_model_evidence_not_replaced_by_evaluation_window(self):
        # Reviewer's scenario: router saw 50 history samples for codex,
        # but the evaluation window holds a single settled outcome.
        codex = _ranking_entry("codex", blended=0.9, etqs=710.0,
                               samples=50, rank=1)
        opencode = _ranking_entry("opencode", blended=0.7, etqs=980.0,
                                  samples=5, rank=2)
        payload = {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": "run-m",
            "task_id": "t-m",
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "recommended_agent": "codex",
            "same_decision": False,
            "candidate_rankings": [codex, opencode],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-m", agent_id="opencode",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-m")
        _settle_outcome(self.store, self.db_path, "t-m", "run-m",
                        "opencode", success=True)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        buckets = {b["bucket_key"]: b
                   for b in report["data_sufficiency"]}
        codex_bucket = buckets[f"codex/{NODE}/{TASK_TYPE}"]
        self.assertEqual(codex_bucket["model_sample_count"], 50)
        self.assertEqual(codex_bucket["model_data_status"], "sufficient")
        self.assertEqual(codex_bucket["evaluation_sample_count"], 0)
        self.assertEqual(codex_bucket["evaluation_data_status"], "cold")
        opencode_bucket = buckets[f"opencode/{NODE}/{TASK_TYPE}"]
        self.assertEqual(opencode_bucket["evaluation_sample_count"], 1)
        readiness = report["canary_readiness"]
        self.assertNotIn("eligible_bucket_count", readiness)

    def test_filter_applies_before_limit(self):
        for i in range(5):
            _record_decision(
                self.store, task_id=f"t-r{i}", run_id=f"run-r{i}",
                node="review", task_type="docs",
                decided_at=BASE_TS + 1000.0 + i)
        for i in range(3):
            _record_decision(
                self.store, task_id=f"t-f{i}", run_id=f"run-f{i}",
                node=NODE, task_type=TASK_TYPE,
                decided_at=BASE_TS + 50.0 + i)
        # Newest 4 decisions are all review/docs: fetch-then-filter
        # would silently return 0 rows here.
        rows = shadow_evaluation.collect_evaluation_rows(
            self.db_path, node=NODE, task_type=TASK_TYPE, limit=4)
        self.assertEqual(len(rows), 3)
        bundle = shadow_evaluation.run_shadow_evaluation(
            self.db_path,
            filters=shadow_evaluation.ShadowEvaluationFilters(
                node=NODE, task_type=TASK_TYPE, limit=4),
        )
        collection = bundle["report"]["collection"]
        self.assertEqual(collection["matched_rows"], 3)
        self.assertFalse(collection["truncated"])
        self.assertGreaterEqual(
            collection["source_window_size"], 8)

    def test_paired_etqs_compares_same_rows(self):
        for i in range(2):
            _record_decision(
                self.store, task_id=f"t-n{i}", run_id=f"run-n{i}",
                actual="opencode", recommended="opencode",
                actual_blended=0.7, decided_at=BASE_TS + 50.0 + i)
        _record_decision(self.store, task_id="t-p", run_id="run-p",
                         actual="opencode", recommended="opencode",
                         actual_blended=0.9, decided_at=BASE_TS + 60.0)
        _settle_outcome(self.store, self.db_path, "t-p", "run-p",
                        "opencode", success=True, wall=1200.0)
        # Force the settled row's frozen ETQS to 1200s so the paired
        # comparison is exact while the overall P50 stays skewed.
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        for row in rows:
            if row["task_id"] == "t-p":
                row["actual_prediction"]["etqs_seconds"] = 1200.0
            else:
                row["actual_prediction"]["etqs_seconds"] = 500.0
        report = shadow_evaluation.build_shadow_evaluation_report(rows)
        etqs = report["etqs"]
        # Only the settled execution evaluates: the two unsettled 500s
        # predictions never enter any headline population.
        self.assertAlmostEqual(
            etqs["predicted_actual_agent_etqs_p50"], 1200.0)
        self.assertAlmostEqual(
            etqs["paired_predicted_etqs_p50"], 1200.0)
        self.assertAlmostEqual(
            etqs["paired_observed_wall_time_p50"], 1200.0)


class CloseoutRegressionTest(unittest.TestCase):
    """PR #101 closeout: identity, dedup, median, pagination bounds."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_caseA_agent_mismatch_never_attaches(self):
        _record_decision(self.store, task_id="T", run_id="R",
                         actual="opencode", recommended="opencode",
                         decided_at=BASE_TS + 50.0)
        _record_decision(self.store, task_id="T", run_id="R",
                         actual="codex", recommended="codex",
                         decided_at=BASE_TS + 60.0)
        _settle_outcome(self.store, self.db_path, "T", "R", "codex")
        bundle = shadow_evaluation.run_shadow_evaluation(self.db_path)
        opencode_row = next(r for r in bundle["rows"]
                            if r["actual_agent"] == "opencode")
        codex_row = next(r for r in bundle["rows"]
                         if r["actual_agent"] == "codex")
        self.assertIsNone(opencode_row["actual_outcome"])
        self.assertIsNotNone(codex_row["actual_outcome"])
        self.assertEqual(len(bundle["execution_rows"]), 1)
        self.assertEqual(
            bundle["execution_rows"][0]["actual_agent"], "codex")
        report = bundle["report"]
        self.assertEqual(report["calibration"]["n"], 1)
        self.assertEqual(report["coverage"]["settled_executions"], 1)
        self.assertEqual(report["coverage"]["unique_executions"], 1)

    def test_node_mismatch_never_attaches(self):
        _record_decision(self.store, task_id="t-n", run_id="run-n",
                         node="implementation", task_type=TASK_TYPE)
        _settle_outcome(self.store, self.db_path, "t-n", "run-n",
                        "opencode", node="review", task_type=TASK_TYPE)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])

    def test_task_type_mismatch_never_attaches(self):
        _record_decision(self.store, task_id="t-t", run_id="run-t",
                         node=NODE, task_type="fix")
        _settle_outcome(self.store, self.db_path, "t-t", "run-t",
                        "opencode", node=NODE, task_type="docs")
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["actual_outcome"])

    def test_dedup_same_agent_keeps_latest_frozen_prediction(self):
        for i, blended in enumerate((0.5, 0.6, 0.7)):
            _record_decision(self.store, task_id="t-e", run_id="run-e",
                             actual="opencode", recommended="opencode",
                             actual_blended=blended,
                             decided_at=BASE_TS + 50.0 + i * 10.0)
        _settle_outcome(self.store, self.db_path, "t-e", "run-e",
                        "opencode", success=True)
        bundle = shadow_evaluation.run_shadow_evaluation(self.db_path)
        self.assertEqual(len(bundle["rows"]), 3)
        self.assertEqual(len(bundle["execution_rows"]), 1)
        only = bundle["execution_rows"][0]
        self.assertAlmostEqual(only["decision_at"], BASE_TS + 70.0)
        self.assertAlmostEqual(
            only["actual_prediction"]["blended_success_rate"], 0.7)

    def test_dedup_retry_picks_latest_matching_agent(self):
        _record_decision(self.store, task_id="t-r", run_id="run-r",
                         actual="opencode", recommended="codex",
                         actual_blended=0.2,
                         decided_at=BASE_TS + 50.0)
        _record_decision(self.store, task_id="t-r", run_id="run-r",
                         actual="codex", recommended="codex",
                         actual_blended=0.6,
                         decided_at=BASE_TS + 60.0)
        _record_decision(self.store, task_id="t-r", run_id="run-r",
                         actual="codex", recommended="codex",
                         actual_blended=0.9,
                         decided_at=BASE_TS + 70.0)
        _settle_outcome(self.store, self.db_path, "t-r", "run-r",
                        "codex", success=True)
        bundle = shadow_evaluation.run_shadow_evaluation(self.db_path)
        self.assertEqual(len(bundle["execution_rows"]), 1)
        only = bundle["execution_rows"][0]
        self.assertEqual(only["actual_agent"], "codex")
        self.assertAlmostEqual(only["decision_at"], BASE_TS + 70.0)
        self.assertAlmostEqual(
            only["actual_prediction"]["blended_success_rate"], 0.9)

    def test_median_is_standard_math(self):
        from herdr.shadow_metrics import _median
        self.assertEqual(_median([1]), 1)
        self.assertAlmostEqual(_median([1, 100]), 50.5)
        self.assertEqual(_median([1, 2, 3]), 2)
        self.assertAlmostEqual(_median([1, 2, 3, 4]), 2.5)
        self.assertIsNone(_median([]))

    def test_pagination_stops_at_matched_limit(self):
        for i in range(10):
            _record_decision(
                self.store, task_id=f"t-m{i}", run_id=f"run-m{i}",
                decided_at=BASE_TS + 50.0 + i)
        bundle = shadow_evaluation.run_shadow_evaluation(
            self.db_path,
            filters=shadow_evaluation.ShadowEvaluationFilters(limit=3),
        )
        collection = bundle["report"]["collection"]
        self.assertEqual(collection["matched_rows"], 3)
        self.assertEqual(collection["stop_reason"], "matched_limit")
        self.assertFalse(collection["truncated"])

    def test_pagination_finds_deep_matches(self):
        for i in range(100):
            _record_decision(
                self.store, task_id=f"t-x{i}", run_id=f"run-x{i}",
                node="review", task_type="docs",
                decided_at=BASE_TS + 1000.0 + i)
        for i in range(2):
            _record_decision(
                self.store, task_id=f"t-f{i}", run_id=f"run-f{i}",
                node=NODE, task_type=TASK_TYPE,
                decided_at=BASE_TS + 50.0 + i)
        rows = shadow_evaluation.collect_evaluation_rows(
            self.db_path, node=NODE, task_type=TASK_TYPE, limit=2,
            page_size=20)
        self.assertEqual(len(rows), 2)

    def test_scan_cap_is_hard_bound(self):
        from herdr.shadow_rows import _collect_rows_with_meta
        for i in range(8):
            _record_decision(
                self.store, task_id=f"t-c{i}", run_id=f"run-c{i}",
                decided_at=BASE_TS + 50.0 + i)
        rows, meta = _collect_rows_with_meta(
            self.db_path, limit=100, page_size=1000, scan_cap=5)
        self.assertLessEqual(meta["source_window_size"], 5)
        self.assertEqual(meta["stop_reason"], "scan_cap")
        self.assertTrue(meta["truncated"])
        self.assertLessEqual(len(rows), 5)
        _, meta_one = _collect_rows_with_meta(
            self.db_path, limit=100, page_size=1000, scan_cap=1)
        self.assertLessEqual(meta_one["source_window_size"], 1)

    def test_scan_cap_splits_pages(self):
        from herdr.shadow_rows import _collect_rows_with_meta
        for i in range(60):
            _record_decision(
                self.store, task_id=f"t-p{i}", run_id=f"run-p{i}",
                decided_at=BASE_TS + 50.0 + i)
        _, meta = _collect_rows_with_meta(
            self.db_path, limit=100, page_size=20, scan_cap=50)
        self.assertLessEqual(meta["source_window_size"], 50)
        self.assertEqual(meta["stop_reason"], "scan_cap")

    def test_scan_cap_exact_page_split(self):
        from herdr.shadow_rows import _collect_rows_with_meta
        for i in range(40):
            _record_decision(
                self.store, task_id=f"t-e{i}", run_id=f"run-e{i}",
                decided_at=BASE_TS + 50.0 + i)
        _, meta = _collect_rows_with_meta(
            self.db_path, limit=100, page_size=20, scan_cap=30)
        # 20 + 10, never 20 + 20.
        self.assertEqual(meta["source_window_size"], 30)
        self.assertEqual(meta["stop_reason"], "scan_cap")


    def test_model_evidence_survives_without_outcome(self):
        codex = _ranking_entry("codex", blended=0.9, etqs=710.0,
                               samples=50, rank=1)
        opencode = _ranking_entry("opencode", blended=0.7, etqs=980.0,
                                  samples=5, rank=2)
        payload = {
            "mode": "shadow",
            "workflow_id": "wf-shadow",
            "run_id": "run-o",
            "task_id": "t-o",
            "node": NODE,
            "task_type": TASK_TYPE,
            "actual_agent": "opencode",
            "recommended_agent": "codex",
            "same_decision": False,
            "candidate_rankings": [codex, opencode],
            "algorithm_version": adaptive_router.ALGORITHM_VERSION,
            "created_at": BASE_TS + 50.0,
        }
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-shadow",
            node_id=NODE, task_id="t-o", agent_id="opencode",
            source="adaptive-router-shadow", timestamp=BASE_TS + 50.0,
            run_id="run-o")
        # Deliberately no settled outcome for this decision.
        bundle = shadow_evaluation.run_shadow_evaluation(self.db_path)
        self.assertEqual(len(bundle["rows"]), 1)
        self.assertEqual(len(bundle["execution_rows"]), 0)
        buckets = {b["bucket_key"]: b
                   for b in bundle["report"]["data_sufficiency"]}
        codex_bucket = buckets[f"codex/{NODE}/{TASK_TYPE}"]
        self.assertEqual(codex_bucket["model_sample_count"], 50)
        self.assertEqual(codex_bucket["model_data_status"], "sufficient")
        self.assertEqual(codex_bucket["evaluation_sample_count"], 0)
        self.assertEqual(codex_bucket["evaluation_data_status"], "cold")


class ShadowEvalCliTest(unittest.TestCase):
    """Real CLI chain: herdr-task shadow-eval -> core -> db -> report."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_invalid_args_never_touch_db(self):
        import subprocess as _subprocess
        import sys as _sys

        root = Path(__file__).resolve().parent.parent
        fresh_db = Path(tempfile.mkdtemp(prefix="herdr-shadow-ro-")) / "fresh.db"
        env = dict(_os.environ, HERDR_STATE_DB=str(fresh_db))
        self.assertFalse(fresh_db.exists())
        for bad in (["--since", "abc"], ["--limit", "xyz"],
                    ["--limit", "0"], ["--limit", "-1"]):
            proc = _subprocess.run(
                [_sys.executable, str(root / "bin" / "herdr-task"),
                 "shadow-eval"] + bad,
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            # Read-only promise: arg validation precedes any DB access, so
            # no database file (schema init/migration) may appear.
            self.assertFalse(fresh_db.exists())
        # Case 1: an absent DB is a hard error, never an empty report.
        # The read-only diagnostic must not create the database file it
        # was asked to observe.
        missing = _subprocess.run(
            [_sys.executable, str(root / "bin" / "herdr-task"),
             "shadow-eval", "--json"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(missing.returncode, 0, missing.stderr)
        self.assertIn("state database not found", missing.stderr)
        self.assertFalse(fresh_db.exists())

    def test_cli_text_and_json_are_read_only(self):
        import subprocess as _subprocess
        import sys as _sys

        _record_decision(self.store, task_id="t-cli", run_id="run-cli",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-cli", "run-cli",
                        "opencode", success=True)
        root = Path(__file__).resolve().parent.parent
        env = dict(_os.environ, HERDR_STATE_DB=str(self.db_path))
        text = _subprocess.run(
            [_sys.executable, str(root / "bin" / "herdr-task"),
             "shadow-eval"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Adaptive Router Shadow Evaluation", text.stdout)
        self.assertIn("opencode -> codex", text.stdout)
        as_json = _subprocess.run(
            [_sys.executable, str(root / "bin" / "herdr-task"),
             "shadow-eval", "--json"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(as_json.returncode, 0, as_json.stderr)
        report = _json.loads(as_json.stdout)
        self.assertEqual(report["coverage"]["total_route_decisions"], 1)
        self.assertEqual(
            report["coverage"]["route_decisions_with_outcome"], 1)
        self.assertEqual(report["agreement"]["different_decision_count"], 1)
        # Read-only: the CLI wrote no decisions and no outcomes.
        self.assertEqual(
            len(state_db.query_route_decisions(db_path=self.db_path)), 1)
        self.assertEqual(
            len(state_db.query_execution_outcomes(
                agents=None, node=NODE, task_type=TASK_TYPE,
                before=BASE_TS + 1_000_000.0, db_path=self.db_path)), 1)


def _db_sidecars(db_path):
    """Sidecar files a read-only run must never leave behind.

    SQLite may transiently materialize ``-wal``/``-shm`` while reading a
    WAL-mode DB and removes them again on a clean last-connection close
    (OS-level coordination, not HAFlow state); the assertions using this
    helper target absent or rollback-journal databases, where such files
    cannot appear at all. Note: the production StateStore legitimately
    creates ``state.db.schema.lock`` when a test fixture first builds
    the DB; that belongs to the write path, not to the shadow read under
    test, so "untouched" assertions below compare before/after file sets
    instead of demanding absence.
    """
    return [
        db_path.parent / f"{db_path.name}{suffix}"
        for suffix in ("-wal", "-shm", ".schema.lock")
    ]


def _assert_no_db_artifacts(test_case, db_path):
    db_path = Path(db_path)
    test_case.assertFalse(
        db_path.exists(), f"read-only run must not create {db_path}")
    for sidecar in _db_sidecars(db_path):
        test_case.assertFalse(
            sidecar.exists(), f"read-only run must not create {sidecar}")


class ShadowEvalTrueReadOnlyTest(unittest.TestCase):
    """PR #102: shadow-eval opens the state DB strictly read-only.

    The boundary is HAFlow persistent state, not the file system in
    general: the run must not create the DB (or its directory), migrate
    schema, write schema_meta, flip the journal mode, or leave a schema
    lock file. SQLite's own ``-wal``/``-shm`` coordination files are
    tolerated by design -- they are transient WAL bookkeeping SQLite
    owns and removes on a clean close, and gating them with a pre-open
    existence check would be a TOCTOU race anyway.
    """

    def setUp(self):
        import sqlite3 as _sqlite3

        self._sqlite3 = _sqlite3
        self.store, self.db_path = _make_env(self)
        self.addCleanup(
            lambda: _os.chmod(self.db_path, 0o644)
            if self.db_path.exists() else None)
        self.root = Path(__file__).resolve().parent.parent

    def _run_shadow_eval(self, db_path, *extra):
        import subprocess as _subprocess
        import sys as _sys

        env = dict(_os.environ, HERDR_STATE_DB=str(db_path))
        return _subprocess.run(
            [_sys.executable, str(self.root / "bin" / "herdr-task"),
             "shadow-eval"] + list(extra),
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_case1_missing_db_fails_without_creating_anything(self):
        workdir = Path(tempfile.mkdtemp(prefix="herdr-shadow-missing-"))
        db_path = workdir / "sub" / "state.db"
        before = sorted(
            str(p) for p in workdir.rglob("*") if p.is_file())
        proc = self._run_shadow_eval(db_path, "--json")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("state database not found", proc.stderr)
        self.assertFalse(db_path.exists())
        self.assertFalse((workdir / "sub").exists())
        for sidecar in _db_sidecars(db_path):
            self.assertFalse(sidecar.exists())
        # Nothing else may appear in the directory either.
        self.assertEqual(
            sorted(str(p) for p in workdir.rglob("*") if p.is_file()),
            before)

    def test_case2_invalid_args_fail_before_any_db_access(self):
        workdir = Path(tempfile.mkdtemp(prefix="herdr-shadow-invalid-"))
        db_path = workdir / "state.db"
        for bad in (["--since", "nope"], ["--limit", "0"],
                    ["--limit", "-3"]):
            proc = self._run_shadow_eval(db_path, *bad)
            self.assertEqual(proc.returncode, 2, proc.stderr)
            _assert_no_db_artifacts(self, db_path)

    def test_case4_query_leaves_schema_rows_and_metadata_untouched(self):
        _record_decision(self.store, task_id="t-ro", run_id="run-ro",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-ro", "run-ro",
                        "opencode", success=True)

        def snapshot():
            # One short-lived writable inspection connection (opened and
            # closed around the snapshot) plus raw file bytes: the state
            # the shadow-eval CLI must not change is HAFlow state -- DDL,
            # object names, row counts, schema_meta rows and the
            # persisted database bytes.
            probe = self._sqlite3.connect(str(self.db_path), timeout=10.0)
            try:
                ddl = sorted(
                    row[0] for row in probe.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE sql IS NOT NULL"))
                names = sorted(
                    row[0] for row in probe.execute(
                        "SELECT type || ':' || name FROM sqlite_master"))
                tables = [
                    row[0] for row in probe.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'")]
                counts = {
                    table: probe.execute(
                        f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in tables
                }
                meta = sorted(
                    tuple(row) for row in probe.execute(
                        "SELECT * FROM schema_meta"))
            finally:
                probe.close()
            return {
                "ddl": ddl, "names": names, "counts": counts,
                "schema_meta": meta,
                "bytes": self.db_path.read_bytes(),
            }

        before = snapshot()
        files_before = sorted(
            str(p) for p in self.db_path.parent.rglob("*") if p.is_file())
        proc = self._run_shadow_eval(self.db_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json.loads(proc.stdout)
        self.assertEqual(report["coverage"]["total_route_decisions"], 1)
        after = snapshot()
        self.assertEqual(after, before)
        # The read-only run left no new files behind (no -wal / -shm /
        # .schema.lock of its own; the fixture's own lock predates it).
        self.assertEqual(
            sorted(
                str(p)
                for p in self.db_path.parent.rglob("*") if p.is_file()),
            files_before)
        # Journal mode still serves reads and was never reconfigured.
        conn = self._sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_case3_quiesced_wal_db_reads_with_app_data_unchanged(self):
        # Rescoped contract (PR #102 review): the read-only promise
        # covers HAFlow persistent state, not SQLite's own coordination
        # files. A quiesced WAL DB (no live connection -> no -wal/-shm
        # right now) must read successfully: SQLite may materialize the
        # pair for the run's duration and removes it again on a clean
        # last-connection close. Gating that with a pre-open existence
        # check would be a TOCTOU race, so the contract deliberately
        # tolerates the files -- while application data stays strictly
        # invariant.
        _record_decision(self.store, task_id="t-quiet", run_id="run-quiet",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-quiet", "run-quiet",
                        "opencode", success=True)
        wal = self.db_path.parent / "state.db-wal"
        shm = self.db_path.parent / "state.db-shm"
        with open(self.db_path, "rb") as handle:
            header = handle.read(20)
        self.assertEqual(header[:16], b"SQLite format 3\x00")
        self.assertEqual((header[18], header[19]), (2, 2))  # WAL header
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        proc = self._run_shadow_eval(self.db_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json.loads(proc.stdout)
        self.assertEqual(report["coverage"]["total_route_decisions"], 1)
        # Application data invariance: rows unchanged and the journal
        # mode was never reconfigured by the read.
        self.assertEqual(
            len(state_db.query_route_decisions(db_path=self.db_path)), 1)
        conn = self._sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()
        # SQLite's coordination files are SQLite's own business: a clean
        # last-connection close removed them again -- no residue.
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_case5_legacy_schema_fails_without_migrating(self):
        workdir = Path(tempfile.mkdtemp(prefix="herdr-shadow-legacy-"))
        db_path = workdir / "state.db"
        conn = self._sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workflow_id TEXT, task_id TEXT, event_type TEXT, "
            "payload_json TEXT, timestamp REAL)")
        conn.commit()
        ddl_before = sorted(
            row[0] for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        conn.close()
        proc = self._run_shadow_eval(db_path, "--json")
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not compatible with shadow evaluation", proc.stderr)
        conn = self._sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro", uri=True)
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("agent_execution_outcomes", tables)
            ddl_after = sorted(
                row[0] for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
            self.assertEqual(ddl_after, ddl_before)
        finally:
            conn.close()
        for sidecar in _db_sidecars(db_path):
            self.assertFalse(sidecar.exists())

    def test_case6_read_only_file_still_serves_queries(self):
        _record_decision(self.store, task_id="t-chmod", run_id="run-chmod",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-chmod", "run-chmod",
                        "opencode", success=True)
        _os.chmod(self.db_path, 0o444)
        proc = self._run_shadow_eval(self.db_path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = _json.loads(proc.stdout)
        self.assertEqual(report["coverage"]["total_route_decisions"], 1)

    def test_case7_readonly_connection_blocks_writes(self):
        _record_decision(self.store, task_id="t-block", run_id="run-block")
        conn = state_db.get_readonly_db_connection(self.db_path)
        try:
            self.assertEqual(
                conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(self._sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO events (workflow_id) VALUES ('nope')")
            with self.assertRaises(self._sqlite3.OperationalError):
                conn.execute("CREATE TABLE shadow_probe (id INTEGER)")
        finally:
            conn.close()

    def test_readonly_connection_never_touches_a_missing_db(self):
        workdir = Path(tempfile.mkdtemp(prefix="herdr-shadow-never-"))
        db_path = workdir / "nested" / "state.db"
        with self.assertRaises(FileNotFoundError):
            state_db.get_readonly_db_connection(db_path)
        self.assertFalse(db_path.exists())
        self.assertFalse((workdir / "nested").exists())
        for sidecar in _db_sidecars(db_path):
            self.assertFalse(sidecar.exists())

    def test_shadow_chain_never_uses_the_writable_connection(self):
        _record_decision(self.store, task_id="t-chain", run_id="run-chain",
                         actual="opencode", recommended="codex")
        _settle_outcome(self.store, self.db_path, "t-chain", "run-chain",
                        "opencode", success=True)
        writable = patch.object(
            state_db, "get_db_connection",
            side_effect=AssertionError(
                "shadow read opened a writable connection"))
        initializer = patch.object(
            state_db, "init_db",
            side_effect=AssertionError(
                "shadow read triggered schema init"))
        writable.start()
        self.addCleanup(writable.stop)
        initializer.start()
        self.addCleanup(initializer.stop)
        rows = shadow_evaluation.collect_evaluation_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        bundle = shadow_evaluation.run_shadow_evaluation(self.db_path)
        self.assertEqual(
            bundle["report"]["coverage"]["total_route_decisions"], 1)

    def test_production_writable_connection_behavior_is_unchanged(self):
        workdir = Path(tempfile.mkdtemp(prefix="herdr-shadow-prod-"))
        db_path = workdir / "deep" / "state.db"
        conn = state_db.get_db_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, event_type) "
                "VALUES ('wf-prod', 'route_decision')")
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main()
