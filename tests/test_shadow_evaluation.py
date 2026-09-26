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
        self.assertAlmostEqual(
            etqs["predicted_actual_agent_etqs_p50"], 500.0)
        self.assertAlmostEqual(
            etqs["paired_predicted_etqs_p50"], 1200.0)
        self.assertAlmostEqual(
            etqs["paired_observed_wall_time_p50"], 1200.0)


class ShadowEvalCliTest(unittest.TestCase):
    """Real CLI chain: herdr-task shadow-eval -> core -> db -> report."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

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


if __name__ == "__main__":
    unittest.main()
