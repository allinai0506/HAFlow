"""Immutable Agent Execution Outcome Fact Layer tests.

Contract under test (task spec section 26, Cases 1-13):
- herdr/execution_outcome.py: resolve once (pure), finalize once
  (immutable, idempotent), read many.
- herdr/state_db.py: agent_execution_outcomes insert/read/query + bucket
  index (EXPLAIN-locked, no scan, no temp B-tree, no JSON extraction).
- herdr/adaptive_router.py: ranks settled outcomes only.

Auto-finalization hooks are OFF in this file (HERDR_OUTCOME_AUTOFINALIZE=0)
so seeded historical timestamps stay controlled; every settlement goes
through the explicit canonical finalize_execution_outcome. Hook wiring is
covered by dedicated tests that re-enable it.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import (
    adaptive_router,
    agent_router,
    eval_store,
    execution_outcome as outcome,
    state_db,
)
from herdr.state_store import get_state_store

BASE_TS = 1_700_000_000.0
CUTOFF = BASE_TS + 100_000.0
NODE = "implementation"
TASK_TYPE = "fix"


def _make_env(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-outcome-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    env_patch = patch.dict(
        os.environ,
        {
            "HERDR_STATE_DB": str(tmp_path / "state.db"),
            "HERDR_OUTCOME_AUTOFINALIZE": "0",
        },
    )
    env_patch.start()
    test.addCleanup(env_patch.stop)
    store = get_state_store(tmp_path / "state.db")
    router_patchers = [
        patch("herdr.agent_router._get_store", return_value=store),
        patch("herdr.agent_router.POOLS_FILE", tmp_path / "agent-pools.json"),
        patch("herdr.agent_router.RESERVATIONS_FILE", tmp_path / "agent-reservations.json"),
        patch("herdr.agent_router.ROUTER_LOCK_FILE", tmp_path / "agent-router.lock"),
    ]
    for p in router_patchers:
        p.start()
        test.addCleanup(p.stop)
    return store, tmp_path / "state.db"


def _save_task(store, task_id, run_id, agent, *, status="completed",
               node=NODE, task_type=TASK_TYPE, wall=600.0, ts=None,
               rework=False, blocked=False, started=True, finished=True):
    ts = BASE_TS if ts is None else ts
    history = ["pending", "dispatched", "working"]
    if blocked:
        history += ["blocked", "working"]
    history.append("agent_done")
    if rework:
        history += ["rework", "working", "agent_done"]
    history.append(status)
    task = {
        "task_id": task_id,
        "workflow_id": "wf-hist",
        "run_id": run_id,
        "node": node,
        "stage": node,
        "task_type": task_type,
        "agent": agent,
        "status": status,
        "stage_verdict": "pass",
        "status_history": [{"to": s} for s in history],
        "created_at": ts,
    }
    if started:
        task["started_at"] = ts
    if finished:
        task["finished_at"] = ts + wall
    store.save_task(task)
    return task


def _record_eval(db_path, run_id, task_id, *, success=True,
                 verification=True, final_status="completed", human=0,
                 created_at=None, revision=None):
    return eval_store.record_eval_result(
        run_id,
        requirements_satisfied=bool(success),
        verification_passed=bool(verification),
        human_intervention_count=int(human),
        final_status=final_status,
        task_id=task_id,
        workflow_id="wf-hist",
        created_at=BASE_TS + 700.0 if created_at is None else created_at,
        revision=revision,
        db_path=db_path,
    )


def _settle(db_path, task_id, *, at=None):
    return outcome.finalize_execution_outcome(
        task_id, db_path=db_path,
        finalized_at=CUTOFF - 10_000.0 if at is None else at,
    )


class OutcomeSettlementTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_case1_success_creates_qualified_outcome(self):
        _save_task(self.store, "t-1", "run-1", "codex")
        _record_eval(self.db_path, "run-1", "t-1")
        result = _settle(self.db_path, "t-1")
        self.assertEqual(result["status"], "created")
        row = result["outcome"]
        self.assertTrue(row["qualified_success"])
        self.assertEqual(row["agent"], "codex")
        self.assertEqual(row["node"], NODE)
        self.assertEqual(row["task_type"], TASK_TYPE)
        self.assertEqual(row["wall_time_seconds"], 600.0)
        self.assertEqual(row["rework_count"], 0)
        self.assertEqual(row["blocked_count"], 0)
        self.assertEqual(row["source_eval_revision"], 1)
        self.assertEqual(row["schema_version"], 1)

    def test_case2_failure_outcome_exists_not_qualified(self):
        _save_task(self.store, "t-2a", "run-2a", "codex", status="failed")
        _record_eval(self.db_path, "run-2a", "t-2a", success=False,
                     final_status="failed")
        failed = _settle(self.db_path, "t-2a")
        self.assertEqual(failed["status"], "created")
        self.assertFalse(failed["outcome"]["qualified_success"])
        _save_task(self.store, "t-2b", "run-2b", "codex")
        _record_eval(self.db_path, "run-2b", "t-2b", success=True,
                     verification=False, final_status="completed")
        unverified = _settle(self.db_path, "t-2b")
        self.assertEqual(unverified["status"], "created")
        self.assertFalse(unverified["outcome"]["qualified_success"])

    def test_case3_incomplete_eval_creates_nothing(self):
        _save_task(self.store, "t-3a", "run-3a", "codex")
        _record_eval(self.db_path, "run-3a", "t-3a", final_status=None)
        result = _settle(self.db_path, "t-3a")
        self.assertEqual(result["status"], "not_ready")
        self.assertIsNone(result["outcome"])
        self.assertIsNone(
            outcome.get_execution_outcome("t-3a", "run-3a", self.db_path))
        # And a missing eval is equally ungradeable.
        _save_task(self.store, "t-3b", "run-3b", "codex")
        missing = _settle(self.db_path, "t-3b")
        self.assertEqual(missing["status"], "not_ready")
        self.assertEqual(missing["reason"], "eval_missing")

    def test_case4_shared_run_taskless_eval_settles_nothing(self):
        _save_task(self.store, "t-4a", "run-shared", "codex")
        _save_task(self.store, "t-4b", "run-shared", "opencode")
        eval_store.record_eval_result(
            "run-shared", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status="completed", task_id=None, workflow_id="wf-hist",
            created_at=BASE_TS + 700.0, db_path=self.db_path,
        )
        for task_id in ("t-4a", "t-4b"):
            result = _settle(self.db_path, task_id)
            self.assertEqual(result["status"], "not_ready", task_id)
            self.assertIsNone(result["outcome"])

    def test_case5_task_specific_evals_settle_independently(self):
        _save_task(self.store, "t-5a", "run-5", "codex")
        _save_task(self.store, "t-5b", "run-5", "opencode")
        _record_eval(self.db_path, "run-5", "t-5a", success=True)
        _record_eval(self.db_path, "run-5", "t-5b", success=False,
                     final_status="failed")
        settled_a = _settle(self.db_path, "t-5a")
        settled_b = _settle(self.db_path, "t-5b")
        self.assertEqual(settled_a["status"], "created")
        self.assertEqual(settled_b["status"], "created")
        self.assertTrue(settled_a["outcome"]["qualified_success"])
        self.assertFalse(settled_b["outcome"]["qualified_success"])

    def test_case6_revisions_do_not_cross_attribute(self):
        _save_task(self.store, "t-6a", "run-6", "codex")
        _save_task(self.store, "t-6b", "run-6", "opencode")
        _record_eval(self.db_path, "run-6", "t-6a", success=True,
                     created_at=BASE_TS + 100.0, revision=1)
        _record_eval(self.db_path, "run-6", "t-6b", success=False,
                     final_status="failed", created_at=BASE_TS + 200.0,
                     revision=2)
        settled_a = _settle(self.db_path, "t-6a")
        settled_b = _settle(self.db_path, "t-6b")
        self.assertEqual(settled_a["outcome"]["source_eval_revision"], 1)
        self.assertEqual(settled_b["outcome"]["source_eval_revision"], 2)
        self.assertTrue(settled_a["outcome"]["qualified_success"])
        self.assertFalse(settled_b["outcome"]["qualified_success"])

    def test_case7_outcome_immutable_after_mutation(self):
        _save_task(self.store, "t-7", "run-7", "codex")
        _record_eval(self.db_path, "run-7", "t-7", success=True)
        first = _settle(self.db_path, "t-7")
        self.assertEqual(first["status"], "created")
        snapshot = dict(first["outcome"])
        # Later mutations: history grows, task timestamp moves, a new
        # (failing) eval revision lands. The settled row must not move.
        task = self.store.get_task("t-7")
        task["status_history"].append({"to": "rework"})
        task["status_history"].append({"to": "failed"})
        self.store.save_task(task)
        _record_eval(self.db_path, "run-7", "t-7", success=False,
                     final_status="failed", created_at=CUTOFF + 500.0)
        reread = outcome.get_execution_outcome("t-7", "run-7", self.db_path)
        self.assertEqual(reread, snapshot)
        again = outcome.finalize_execution_outcome(
            "t-7", db_path=self.db_path, finalized_at=CUTOFF + 600.0)
        self.assertEqual(again["status"], "exists")
        self.assertEqual(again["outcome"], snapshot)

    def test_case8_finalize_idempotent(self):
        _save_task(self.store, "t-8", "run-8", "codex")
        _record_eval(self.db_path, "run-8", "t-8")
        first = _settle(self.db_path, "t-8")
        second = _settle(self.db_path, "t-8")
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "exists")
        self.assertEqual(first["outcome"], second["outcome"])
        rows = state_db.query_execution_outcomes(
            agents=["codex"], node=NODE, task_type=TASK_TYPE,
            before=CUTOFF, db_path=self.db_path)
        self.assertEqual(
            sum(1 for row in rows if row["task_id"] == "t-8"), 1)

    def test_case9_cutoff_reads_only_earlier_outcomes(self):
        _save_task(self.store, "t-9a", "run-9a", "codex", ts=BASE_TS)
        _record_eval(self.db_path, "run-9a", "t-9a", created_at=BASE_TS + 1.0)
        _settle(self.db_path, "t-9a", at=BASE_TS + 10.0)  # 10:00
        _save_task(self.store, "t-9b", "run-9b", "codex", ts=BASE_TS + 20.0)
        _record_eval(self.db_path, "run-9b", "t-9b", created_at=BASE_TS + 21.0)
        _settle(self.db_path, "t-9b", at=BASE_TS + 7_200.0)  # 12:00
        rankings = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=BASE_TS + 3_600.0, active_loads={}, reserved_loads={},
        )
        self.assertEqual(rankings[0]["sample_count"], 1)
        rows = adaptive_router.collect_samples(
            self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=BASE_TS + 3_600.0, agents=["codex"])
        self.assertEqual([row["task_id"] for row in rows], ["t-9a"])

    def test_case10_per_agent_windows_not_crowded_out(self):
        for i in range(30):
            task_id, run_id = f"t-10k-{i}", f"run-10k-{i}"
            _save_task(self.store, task_id, run_id, "kimi", ts=BASE_TS + i)
            _record_eval(self.db_path, run_id, task_id,
                         created_at=BASE_TS + i + 0.5)
            _settle(self.db_path, task_id, at=BASE_TS + i + 1.0)
        for i in range(3):
            task_id, run_id = f"t-10c-{i}", f"run-10c-{i}"
            _save_task(self.store, task_id, run_id, "codex",
                       ts=BASE_TS + 100 + i)
            _record_eval(self.db_path, run_id, task_id,
                         created_at=BASE_TS + 100 + i + 0.5)
            _settle(self.db_path, task_id, at=BASE_TS + 100 + i + 1.0)
        rankings = adaptive_router.rank_candidates(
            ["kimi", "codex"], db_path=self.db_path, node=NODE,
            task_type=TASK_TYPE, cutoff=CUTOFF, active_loads={},
            reserved_loads={}, limit=5,
        )
        by_agent = {row["agent"]: row for row in rankings}
        self.assertEqual(by_agent["kimi"]["sample_count"], 5)
        self.assertEqual(by_agent["codex"]["sample_count"], 3)

    def test_case11_working_tasks_never_settle(self):
        _save_task(self.store, "t-11w", "run-11w", "codex", status="working",
                   rework=True)
        pending = outcome.finalize_execution_outcome(
            "t-11w", db_path=self.db_path, finalized_at=CUTOFF - 1.0)
        self.assertEqual(pending["status"], "not_ready")
        self.assertEqual(pending["reason"], "task_not_terminal")
        _save_task(self.store, "t-11s", "run-11s", "codex")
        _record_eval(self.db_path, "run-11s", "t-11s")
        _settle(self.db_path, "t-11s")
        rankings = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=CUTOFF, active_loads={}, reserved_loads={},
        )
        # The working rework-heavy task cannot dilute settled rates.
        self.assertEqual(rankings[0]["sample_count"], 1)
        self.assertEqual(rankings[0]["rework_rate"], 0.0)

    def test_wall_time_missing_endpoints_stays_null(self):
        _save_task(self.store, "t-wn", "run-wn", "codex", finished=False)
        _record_eval(self.db_path, "run-wn", "t-wn")
        result = _settle(self.db_path, "t-wn")
        self.assertEqual(result["status"], "created")
        self.assertIsNone(result["outcome"]["wall_time_seconds"])
        self.assertTrue(result["outcome"]["qualified_success"])


class OutcomeReadinessTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_finalization_picks_latest_owned_eval_before_cutoff(self):
        # Write-time freezing: finalizing at T sees rev1; a later rev2
        # cannot rewrite the settled row (contrast with query-time MAX).
        _save_task(self.store, "t-r", "run-r", "codex")
        _record_eval(self.db_path, "run-r", "t-r", success=True,
                     created_at=CUTOFF - 100.0)
        _record_eval(self.db_path, "run-r", "t-r", success=False,
                     verification=False, final_status="failed",
                     created_at=CUTOFF + 100.0)
        early = outcome.finalize_execution_outcome(
            "t-r", db_path=self.db_path, finalized_at=CUTOFF)
        self.assertEqual(early["status"], "created")
        self.assertTrue(early["outcome"]["qualified_success"])
        self.assertEqual(early["outcome"]["source_eval_revision"], 1)
        late = outcome.finalize_execution_outcome(
            "t-r", db_path=self.db_path, finalized_at=CUTOFF + 200.0)
        self.assertEqual(late["status"], "exists")
        self.assertTrue(late["outcome"]["qualified_success"])

    def test_taskless_unique_run_is_attributed(self):
        _save_task(self.store, "t-u", "run-unique", "codex")
        eval_store.record_eval_result(
            "run-unique", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status="completed", task_id=None, workflow_id="wf-hist",
            created_at=BASE_TS + 700.0, db_path=self.db_path,
        )
        result = _settle(self.db_path, "t-u")
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["outcome"]["qualified_success"])

    def test_rework_and_blocked_counts_frozen(self):
        _save_task(self.store, "t-rb", "run-rb", "codex", rework=True,
                   blocked=True)
        _record_eval(self.db_path, "run-rb", "t-rb")
        result = _settle(self.db_path, "t-rb")
        self.assertEqual(result["outcome"]["rework_count"], 1)
        self.assertEqual(result["outcome"]["blocked_count"], 1)
        rankings = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=CUTOFF, active_loads={}, reserved_loads={},
        )
        self.assertEqual(rankings[0]["rework_rate"], 1.0)
        self.assertEqual(rankings[0]["blocked_rate"], 1.0)


class OutcomeIdentityHotfixTest(unittest.TestCase):
    """PR #100: outcome identity + fact-semantics hotfix (3 items only)."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_outcome_id_binds_task_and_run(self):
        _save_task(self.store, "t-hf1", "run-hf1a", "codex")
        _record_eval(self.db_path, "run-hf1a", "t-hf1")
        first = _settle(self.db_path, "t-hf1")
        self.assertEqual(first["status"], "created")
        self.assertEqual(first["outcome"]["outcome_id"],
                         "outcome_t-hf1_run-hf1a")
        _save_task(self.store, "t-hf1", "run-hf1b", "codex")
        _record_eval(self.db_path, "run-hf1b", "t-hf1")
        second = outcome.finalize_execution_outcome(
            "t-hf1", db_path=self.db_path, finalized_at=CUTOFF - 9_000.0)
        self.assertEqual(second["status"], "created")
        self.assertEqual(second["outcome"]["outcome_id"],
                         "outcome_t-hf1_run-hf1b")
        self.assertIsNotNone(outcome.get_execution_outcome(
            "t-hf1", "run-hf1a", self.db_path))
        self.assertIsNotNone(outcome.get_execution_outcome(
            "t-hf1", "run-hf1b", self.db_path))

    def test_cross_workflow_eval_never_settles(self):
        _save_task(self.store, "t-hf2", "run-hf2", "codex")
        eval_store.record_eval_result(
            "run-hf2", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status="completed", task_id="t-hf2",
            workflow_id="wf-other", created_at=BASE_TS + 700.0,
            db_path=self.db_path,
        )
        result = _settle(self.db_path, "t-hf2")
        self.assertEqual(result["status"], "not_ready")
        self.assertIsNone(result["outcome"])
        self.assertIsNone(outcome.get_execution_outcome(
            "t-hf2", "run-hf2", self.db_path))

    def test_unknown_facts_never_solidify_to_zero(self):
        _save_task(self.store, "t-hf3", "run-hf3", "codex")
        eval_store.record_eval_result(
            "run-hf3", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=None,
            final_status="completed", task_id="t-hf3",
            workflow_id="wf-hist", created_at=BASE_TS + 700.0,
            db_path=self.db_path,
        )
        human_none = _settle(self.db_path, "t-hf3")
        self.assertEqual(human_none["status"], "not_ready")
        self.assertEqual(human_none["reason"], "eval_incomplete")
        task = self.store.get_task("t-hf4")  # absent -> build without history
        self.assertIsNone(task)
        raw = dict(_save_task(self.store, "t-hf4", "run-hf4", "codex"))
        raw.pop("status_history", None)
        self.store.save_task(raw)
        _record_eval(self.db_path, "run-hf4", "t-hf4")
        missing_history = _settle(self.db_path, "t-hf4")
        self.assertEqual(missing_history["status"], "not_ready")
        self.assertEqual(missing_history["reason"], "history_missing")


class OutcomeIndexTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_bucket_index_serves_router_query(self):
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        try:
            plan_rows = conn.execute(
                "EXPLAIN QUERY PLAN SELECT outcome_id "
                "FROM agent_execution_outcomes "
                "WHERE agent = ? AND node = ? AND task_type = ? "
                "AND recorded_at < ? "
                "ORDER BY recorded_at DESC, outcome_id DESC LIMIT ?",
                ("codex", NODE, TASK_TYPE, CUTOFF, 500),
            ).fetchall()
            plan = " ".join(str(row) for row in plan_rows)
        finally:
            conn.close()
        self.assertIn("idx_outcomes_bucket", plan)
        # Covering bucket index: equality prefix + range + ORDER BY/LIMIT
        # served without touching the table, sorting, or JSON extraction.
        self.assertIn("USING COVERING INDEX", plan)
        self.assertNotIn("SCAN", plan)
        self.assertNotIn("TEMP", plan)
        for token in plan.upper().split():
            self.assertNotEqual(token.strip("(),"), "JSON_EXTRACT")


class OutcomeHookTest(unittest.TestCase):
    """Write-path auto-finalization: eval lands or task settles -> row."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def _enable_hooks(self):
        hook_patch = patch.dict(os.environ, {"HERDR_OUTCOME_AUTOFINALIZE": "1"})
        hook_patch.start()
        self.addCleanup(hook_patch.stop)

    def test_eval_write_settles_terminal_task(self):
        self._enable_hooks()
        _save_task(self.store, "t-h1", "run-h1", "codex")
        self.assertIsNone(
            outcome.get_execution_outcome("t-h1", "run-h1", self.db_path))
        _record_eval(self.db_path, "run-h1", "t-h1")
        settled = outcome.get_execution_outcome("t-h1", "run-h1", self.db_path)
        self.assertIsNotNone(settled)
        self.assertTrue(settled["qualified_success"])

    def test_terminal_save_settles_after_late_eval(self):
        self._enable_hooks()
        _save_task(self.store, "t-h2", "run-h2", "codex", status="working")
        _record_eval(self.db_path, "run-h2", "t-h2")
        # Eval first, task not terminal: nothing settles yet.
        self.assertIsNone(
            outcome.get_execution_outcome("t-h2", "run-h2", self.db_path))
        task = self.store.get_task("t-h2")
        task["status"] = "completed"
        task["finished_at"] = BASE_TS + 600.0
        task["status_history"].append({"to": "completed"})
        self.store.save_task(task)
        settled = outcome.get_execution_outcome("t-h2", "run-h2", self.db_path)
        self.assertIsNotNone(settled)
        self.assertTrue(settled["qualified_success"])

    def test_terminal_transition_settles_after_early_eval(self):
        self._enable_hooks()
        _save_task(self.store, "t-h3", "run-h3", "codex", status="working")
        _record_eval(self.db_path, "run-h3", "t-h3")
        self.assertIsNone(
            outcome.get_execution_outcome("t-h3", "run-h3", self.db_path))
        state_db.transition_task(
            "t-h3", "agent_done", reason="agent finished",
            db_path=self.db_path)
        # agent_done is not terminal: still nothing settles.
        self.assertIsNone(
            outcome.get_execution_outcome("t-h3", "run-h3", self.db_path))
        state_db.transition_task(
            "t-h3", "completed", reason="stage gate passed",
            db_path=self.db_path)
        settled = outcome.get_execution_outcome("t-h3", "run-h3", self.db_path)
        self.assertIsNotNone(settled)
        self.assertTrue(settled["qualified_success"])

    def test_backfill_settles_only_provable_rows(self):
        _save_task(self.store, "t-b1", "run-b1", "codex")
        _record_eval(self.db_path, "run-b1", "t-b1")
        _save_task(self.store, "t-b2", "run-b2", "opencode")
        _record_eval(self.db_path, "run-b2", "t-b2", final_status=None)
        _save_task(self.store, "t-b3", "run-b3", "kimi", status="working")
        tallies = outcome.backfill_execution_outcomes(
            db_path=self.db_path, finalized_at=CUTOFF - 5_000.0)
        # Only terminal tasks are scanned; the working task never enters.
        self.assertEqual(tallies["scanned"], 2)
        self.assertEqual(tallies["created"], 1)
        self.assertEqual(tallies["skipped"], 1)
        self.assertIn("eval_incomplete", tallies["skip_reasons"])
        self.assertIsNone(
            outcome.get_execution_outcome("t-b3", "run-b3", self.db_path))
        repeat = outcome.backfill_execution_outcomes(
            db_path=self.db_path, finalized_at=CUTOFF - 5_000.0)
        self.assertEqual(repeat["created"], 0)
        self.assertEqual(repeat["exists"], 1)


class OutcomeShadowInvariantTest(unittest.TestCase):
    """Cases 12-13: shadow never steers production; query faults fail open."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)
        self.wf_id = "wf-shadow-outcome"
        self.store.save_workflow({
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": [],
            "unhealthy_agents": {},
        })

    def _node_cfg(self, preferred):
        return {"nodes": [{"id": NODE,
                           "agent_policy": {"preferred": list(preferred)}}]}

    def _seed(self, idx, agent, *, success=True, wall=600.0, rework=False):
        task_id, run_id = f"t-s-{agent}-{idx}", f"run-s-{agent}-{idx}"
        _save_task(self.store, task_id, run_id, agent, wall=wall,
                   ts=BASE_TS + idx,
                   status="completed" if success else "failed",
                   rework=rework)
        _record_eval(self.db_path, run_id, task_id, success=success,
                     verification=success,
                     final_status="completed" if success else "failed",
                     created_at=BASE_TS + idx + wall + 1.0)
        result = outcome.finalize_execution_outcome(
            task_id, db_path=self.db_path,
            finalized_at=BASE_TS + idx + wall + 5.0)
        self.assertEqual(result["status"], "created")

    def test_case12_shadow_recommendation_does_not_steer(self):
        for i in range(20):
            self._seed(i, "codex", success=(i < 19))
        for i in range(20, 30):
            self._seed(i, "opencode", success=(i < 24), wall=900.0,
                       rework=True)
        with patch("herdr.agent_router.workflow_config_for",
                    return_value=self._node_cfg(["opencode", "codex"])):
            selected = agent_router.choose_agent(
                self.wf_id, NODE, TASK_TYPE, requested="auto",
                reservation_key="task-shadow-12", run_id="run-shadow-12",
            )
        self.assertEqual(selected, "opencode")
        events = self.store.list_events(event_type="route_decision")
        self.assertTrue(events)
        payload = events[-1]["payload"]
        self.assertEqual(payload["actual_agent"], "opencode")
        self.assertEqual(payload["recommended_agent"], "codex")
        self.assertFalse(payload["same_decision"])

    def test_case13_outcome_query_failure_is_fail_open(self):
        with patch("herdr.agent_router.workflow_config_for",
                    return_value=self._node_cfg(["opencode", "codex"])):
            with patch("herdr.state_db.query_execution_outcomes",
                        side_effect=RuntimeError("boom")):
                selected = agent_router.choose_agent(
                    self.wf_id, NODE, TASK_TYPE, requested="auto",
                    reservation_key="task-shadow-13", run_id="run-shadow-13",
                )
        self.assertEqual(selected, "opencode")
        errors = self.store.list_events(event_type="route_decision_error")
        self.assertTrue(errors)
        self.assertIn("boom", str(errors[-1]["payload"].get("error")))


if __name__ == "__main__":
    unittest.main()
