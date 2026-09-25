"""Adaptive Agent Router v1 (Shadow Mode) tests.

Contract under test:
- herdr/adaptive_router.py: deterministic, explainable shadow ranking over
  historical (agent x node/stage x task_type) performance. No LLM, no network,
  no randomness. Unknown outcomes stay unknown.
- herdr/agent_router.choose_agent: production selection is unchanged; shadow
  runs fail-open and persists a route_decision event.
- herdr/state_db.query_adaptive_history: bounded, cutoff-gated history reads.

Cases map to task spec section 19 (Case 1..10).
"""

import json as _json
import tempfile
import time as _time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr import adaptive_router, agent_router, eval_store, state_db
from herdr.state_store import get_state_store
from herdr.transitions import COMPLETED_TASK_STATUSES

BASE_TS = 1_700_000_000.0
CUTOFF = BASE_TS + 100_000.0
NODE = "implementation"
TASK_TYPE = "fix"


def _make_env(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-adaptive-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    store = get_state_store(tmp_path / "state.db")
    test.patchers = [
        patch("herdr.agent_router._get_store", return_value=store),
        patch("herdr.agent_router.POOLS_FILE", tmp_path / "agent-pools.json"),
        patch("herdr.agent_router.RESERVATIONS_FILE", tmp_path / "agent-reservations.json"),
        patch("herdr.agent_router.ROUTER_LOCK_FILE", tmp_path / "agent-router.lock"),
    ]
    for p in test.patchers:
        p.start()
        test.addCleanup(p.stop)
    return store, tmp_path / "state.db"


def _seed_sample(store, db_path, idx, agent, *, success=True,
                 verification=True, wall=600.0, rework=False,
                 blocked=False, human=0, node=NODE, task_type=TASK_TYPE,
                 run_prefix="run", final_status=None, ts=None,
                 with_eval=True):
    """Seed one historical task + (optionally) its eval fact."""
    ts = BASE_TS + idx if ts is None else ts
    run_id = f"{run_prefix}-{agent}-{idx}"
    task_id = f"task-{agent}-{idx}"
    history = ["pending", "dispatched", "working"]
    if blocked:
        history += ["blocked", "working"]
    history.append("agent_done")
    if rework:
        history += ["rework", "working", "agent_done"]
    status = final_status or ("completed" if success else "failed")
    history.append(status)
    store.save_task({
        "task_id": task_id,
        "workflow_id": "wf-hist",
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
    if with_eval:
        eval_store.record_eval_result(
            run_id,
            requirements_satisfied=bool(success),
            verification_passed=bool(verification),
            human_intervention_count=int(human),
            final_status=status,
            task_id=task_id,
            workflow_id="wf-hist",
            created_at=ts + wall + 1.0,
            db_path=db_path,
        )
    return run_id, task_id


def _rank(db_path, candidates, **kwargs):
    params = {
        "node": NODE, "task_type": TASK_TYPE, "cutoff": CUTOFF,
        "active_loads": {}, "reserved_loads": {},
    }
    params.update(kwargs)
    return adaptive_router.rank_candidates(
        list(candidates), db_path=db_path, **params)


class AdaptiveRankingTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_case1_high_success_beats_fast_rework(self):
        for i in range(20):
            _seed_sample(self.store, self.db_path, i, "codex",
                         success=(i < 19), wall=600.0)
        for i in range(20, 30):
            _seed_sample(self.store, self.db_path, i, "opencode",
                         success=(i < 26), verification=(i < 26),
                         wall=300.0, rework=(i >= 22))
        rankings = _rank(self.db_path, ["opencode", "codex"])
        self.assertEqual(rankings[0]["agent"], "codex")
        self.assertEqual(rankings[0]["rank"], 1)
        codex = next(r for r in rankings if r["agent"] == "codex")
        opencode = next(r for r in rankings if r["agent"] == "opencode")
        self.assertAlmostEqual(codex["qualified_success_rate"], 19 / 20)
        self.assertGreater(opencode["rework_rate"], 0.5)
        self.assertLess(codex["etqs_seconds"], opencode["etqs_seconds"])

    def test_case2_cold_start_does_not_win(self):
        _seed_sample(self.store, self.db_path, 1, "agent_new", success=True)
        for i in range(2, 52):
            _seed_sample(self.store, self.db_path, i, "agent_proven",
                         success=(i < 48), wall=600.0)
        rankings = _rank(self.db_path, ["agent_new", "agent_proven"])
        self.assertEqual(rankings[0]["agent"], "agent_proven")
        newcomer = next(r for r in rankings if r["agent"] == "agent_new")
        self.assertLess(newcomer["confidence"], 0.5)

    def test_case3_no_history_falls_back_to_candidate_order(self):
        rankings = _rank(self.db_path, ["opencode", "codex", "claude"])
        self.assertEqual([r["agent"] for r in rankings], ["opencode", "codex", "claude"])
        for row in rankings:
            self.assertEqual(row["sample_count"], 0)
            self.assertIsNone(row["qualified_success_rate"])
            self.assertIn("fallback_reason", row)
        first = _rank(self.db_path, ["opencode", "codex", "claude"])
        second = _rank(self.db_path, ["opencode", "codex", "claude"])
        self.assertEqual(first, second)

    def test_case4_unknown_outcome_is_not_success(self):
        _seed_sample(self.store, self.db_path, 1, "codex", success=True, with_eval=False)
        _seed_sample(self.store, self.db_path, 2, "codex", success=True, with_eval=False)
        rankings = _rank(self.db_path, ["codex", "opencode"])
        codex = next(r for r in rankings if r["agent"] == "codex")
        self.assertEqual(codex["sample_count"], 0)
        self.assertIsNone(codex["qualified_success_rate"])
        # Unknown rows must not silently become the recommendation.
        self.assertEqual(rankings[0]["agent"], "codex")  # tie -> candidate order
        self.assertIn("fallback_reason", rankings[0])

    def test_case5_loaded_agent_ranks_lower(self):
        for i in range(10):
            _seed_sample(self.store, self.db_path, i, "agent_a", success=True, wall=600.0)
        for i in range(10, 20):
            _seed_sample(self.store, self.db_path, i, "agent_b", success=True, wall=600.0)
        rankings = _rank(
            self.db_path, ["agent_a", "agent_b"],
            active_loads={"agent_a": 5},
        )
        self.assertEqual(rankings[0]["agent"], "agent_b")
        loaded = next(r for r in rankings if r["agent"] == "agent_a")
        self.assertGreater(loaded["queue_delay_seconds"], 0)
        self.assertEqual(loaded["queue_delay_source"], "estimated")

    def test_case6_task_type_isolation(self):
        for i in range(10):
            _seed_sample(self.store, self.db_path, i, "codex", success=True,
                         task_type="fix")
        for i in range(10, 18):
            _seed_sample(self.store, self.db_path, i, "opencode", success=(i < 17),
                         task_type="docs")
        fix_rank = _rank(self.db_path, ["opencode", "codex"], task_type="fix")
        self.assertEqual(fix_rank[0]["agent"], "codex")
        docs_rank = _rank(self.db_path, ["codex", "opencode"], task_type="docs")
        self.assertEqual(docs_rank[0]["agent"], "opencode")
        docs_codex = next(r for r in docs_rank if r["agent"] == "codex")
        self.assertEqual(docs_codex["sample_count"], 0)

    def test_case7_stage_isolation(self):
        for i in range(10):
            _seed_sample(self.store, self.db_path, i, "codex", success=True,
                         node="implementation")
        for i in range(10, 16):
            _seed_sample(self.store, self.db_path, i, "claude", success=True,
                         node="review")
        review_rank = _rank(self.db_path, ["codex", "claude"], node="review")
        self.assertEqual(review_rank[0]["agent"], "claude")
        review_codex = next(r for r in review_rank if r["agent"] == "codex")
        self.assertEqual(review_codex["sample_count"], 0)

    def test_case8_current_run_excluded_and_cutoff_respected(self):
        for i in range(5):
            _seed_sample(self.store, self.db_path, i, "codex", success=True)
        current_run, _ = _seed_sample(
            self.store, self.db_path, 100, "codex", success=False,
            verification=False, run_prefix="run-current")
        future_run, _ = _seed_sample(
            self.store, self.db_path, 101, "codex", success=True,
            ts=CUTOFF + 50.0, run_prefix="run-future")
        rankings = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=CUTOFF, exclude_run_id=current_run,
            active_loads={}, reserved_loads={},
        )
        codex = rankings[0]
        self.assertEqual(codex["sample_count"], 5)
        self.assertAlmostEqual(codex["qualified_success_rate"], 1.0)
        rows = adaptive_router.collect_samples(
            self.db_path, node=NODE, task_type=TASK_TYPE, cutoff=CUTOFF,
            exclude_run_id=current_run)
        run_ids = {row["run_id"] for row in rows}
        self.assertNotIn(current_run, run_ids)
        self.assertNotIn(future_run, run_ids)

    def test_ranking_is_deterministic(self):
        for i in range(6):
            _seed_sample(self.store, self.db_path, i, "codex", success=(i < 5))
        for i in range(6, 12):
            _seed_sample(self.store, self.db_path, i, "opencode", success=(i < 10))
        first = _rank(self.db_path, ["opencode", "codex"])
        second = _rank(self.db_path, ["opencode", "codex"])
        self.assertEqual(first, second)

    def test_shadow_decision_payload_shape(self):
        rankings = _rank(self.db_path, ["opencode", "codex"])
        decision = adaptive_router.build_shadow_decision(
            workflow_id="wf-1", run_id="run-1", task_id="task-1",
            node=NODE, task_type=TASK_TYPE, actual_agent="opencode",
            rankings=rankings, created_at=CUTOFF,
        )
        self.assertEqual(decision["mode"], "shadow")
        self.assertEqual(decision["actual_agent"], "opencode")
        self.assertEqual(decision["recommended_agent"], "opencode")
        self.assertTrue(decision["same_decision"])
        self.assertEqual(decision["algorithm_version"], adaptive_router.ALGORITHM_VERSION)
        self.assertEqual(decision["workflow_id"], "wf-1")


class LaunchPathIntegrationTest(unittest.TestCase):
    """P1-1: production launch must persist task_type end to end."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

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

    def test_launch_task_persists_task_type(self):
        module = self._load_module("herdr-task-launch-task-type")
        tmp = Path(self.db_path).parent
        with patch.dict(
            "os.environ",
            {
                "HERDR_STATE_DB": str(self.db_path),
                "TASKS_FILE": str(tmp / "tasks.json"),
                "WORKFLOWS_FILE": str(tmp / "workflows.json"),
            },
        ):
            from herdr.state_store import reset_state_store

            reset_state_store()
            store = get_state_store(self.db_path)
            (tmp / "workflow.json").write_text(
                _json.dumps({"nodes": [{"id": "implementation",
                                        "agent_policy": {}}]}),
                encoding="utf-8",
            )
            store.save_workflow({
                "workflow_id": "wf-launch-tt",
                "status": "running",
                "project_id": "project-launch-tt",
                "project_root": str(tmp),
                "execution": {"mode": "git"},
                "workflow_file": str(tmp / "workflow.json"),
            })
            worker_payload = {
                "clone": str(tmp / "clone"),
                "branch": "agent/t-launch-tt",
                "pane_id": "pane-1",
                "agent_session_id": "sess-1",
                "baseline_untracked": [],
                "baseline_fingerprint": {"tracked": {}, "untracked": {}},
                "baseline_commit": None,
                "onto_branch": None,
            }
            worker_result = SimpleNamespace(
                returncode=0, stdout="HERDR_WORKER_RESULT="
                + _json.dumps(worker_payload), stderr="",
            )
            args = SimpleNamespace(
                task_id="t-launch-tt", workflow_id="wf-launch-tt",
                run_id=None, node="implementation", stage=None,
                agent="auto", task_type="fix", integration_mode="none",
                onto=None, source=str(tmp), goal="fix it", acceptance=[],
                prompt="fix it", test_cmd=None, lint_cmd=None,
                repro_cmd=None, agent_policy=None, allow_reuse=False,
                reuse_reason="",
            )
            with patch.object(
                module, "ensure_stage_topology",
                return_value={"workspace_id": "ws", "anchor_pane_id": "pane-a",
                              "tab_id": "tab", "node_label": "Implementation",
                              "stage_label": "Implementation"},
            ), patch.object(
                module, "acquire_pane_for_task", return_value="pane-pre",
            ), patch.object(
                module, "_preflight_delivery_identity", return_value=None,
            ), patch("subprocess.run", return_value=worker_result), patch.object(
                module, "auto_init_task_loop", return_value=None,
            ), patch.object(
                module, "dispatch_task", return_value=None,
            ):
                module._launch_task(args)

        task = get_state_store(self.db_path).get_task("t-launch-tt")
        self.assertIsNotNone(task)
        # The production record itself must carry task_type (was missing).
        self.assertEqual(task.get("task_type"), "fix")

        found = adaptive_router.collect_samples(
            self.db_path, node="implementation", task_type="fix",
            cutoff=_time.time() + 60.0, agents=[task.get("agent")],
        )
        self.assertTrue(
            any(row["task_id"] == "t-launch-tt" for row in found),
            "launch → save_task → query_adaptive_history chain broken",
        )
        missing = adaptive_router.collect_samples(
            self.db_path, node="implementation", task_type="docs",
            cutoff=_time.time() + 60.0, agents=[task.get("agent")],
        )
        self.assertFalse(
            any(row["task_id"] == "t-launch-tt" for row in missing))
        reset_state_store()


class ShadowIntegrationTest(unittest.TestCase):
    """Cases 9-10: shadow never changes production; failures are fail-open."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)
        self.wf_id = "wf-shadow-int"
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

    def test_case9_shadow_does_not_change_production(self):
        for i in range(20):
            _seed_sample(self.store, self.db_path, i, "codex",
                         success=(i < 19), wall=600.0)
        for i in range(20, 30):
            _seed_sample(self.store, self.db_path, i, "opencode",
                         success=(i < 24), verification=(i < 24),
                         wall=900.0, rework=True)
        with patch("herdr.agent_router.workflow_config_for",
                   return_value=self._node_cfg(["opencode", "codex"])):
            selected = agent_router.choose_agent(
                self.wf_id, NODE, TASK_TYPE, requested="auto",
                reservation_key="task-shadow-9", run_id="run-shadow-9",
            )
        # Legacy order prefers opencode first; shadow must not change that.
        self.assertEqual(selected, "opencode")
        events = self.store.list_events(event_type="route_decision")
        self.assertTrue(events, "shadow route_decision event must persist")
        payload = events[-1]["payload"]
        self.assertEqual(payload["mode"], "shadow")
        self.assertEqual(payload["actual_agent"], "opencode")
        self.assertEqual(payload["recommended_agent"], "codex")
        self.assertFalse(payload["same_decision"])
        self.assertIn("candidate_rankings", payload)
        self.assertEqual(
            payload["algorithm_version"], adaptive_router.ALGORITHM_VERSION)

    def test_case10_shadow_failure_is_fail_open(self):
        with patch("herdr.agent_router.workflow_config_for",
                   return_value=self._node_cfg(["opencode", "codex"])):
            with patch("herdr.adaptive_router.rank_candidates",
                       side_effect=RuntimeError("boom")):
                selected = agent_router.choose_agent(
                    self.wf_id, NODE, TASK_TYPE, requested="auto",
                    reservation_key="task-shadow-10", run_id="run-shadow-10",
                )
        self.assertEqual(selected, "opencode")
        errors = self.store.list_events(event_type="route_decision_error")
        self.assertTrue(errors, "shadow failure must leave an audit event")
        self.assertIn("boom", str(errors[-1]["payload"].get("error")))


class HistoryBoundsTest(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def test_history_read_is_bounded(self):
        for i in range(30):
            _seed_sample(self.store, self.db_path, i, "codex", success=True)
        rows = adaptive_router.collect_samples(
            self.db_path, node=NODE, task_type=TASK_TYPE, cutoff=CUTOFF,
            limit=5)
        self.assertLessEqual(len(rows), 5)
        # Newest-first window: highest idx wins.
        self.assertEqual(rows[0]["task_id"], "task-codex-29")

    def test_completed_status_family_counts_as_success(self):
        for status in sorted(COMPLETED_TASK_STATUSES):
            idx = 100 + sorted(COMPLETED_TASK_STATUSES).index(status)
            _seed_sample(self.store, self.db_path, idx, "codex", success=True,
                         final_status=status)
        rankings = _rank(self.db_path, ["codex"])
        self.assertAlmostEqual(rankings[0]["qualified_success_rate"], 1.0)


class ReviewFixRegressionTest(unittest.TestCase):
    """PR #99 reviewer fixes: trust the history fact chain."""

    def setUp(self):
        self.store, self.db_path = _make_env(self)

    def _unique_task(self, idx, agent, run_id, **kwargs):
        ts = BASE_TS + idx
        task_id = f"task-manual-{idx}"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-hist",
            "run_id": run_id,
            "node": NODE,
            "stage": NODE,
            "task_type": TASK_TYPE,
            "agent": agent,
            "status": kwargs.get("status", "completed"),
            "stage_verdict": "pass",
            "acceptance_verdict": True,
            "status_history": [{"to": s} for s in ("pending", "agent_done", "completed")],
            "started_at": ts,
            "finished_at": ts + 600.0,
            "created_at": ts,
        }
        self.store.save_task(task)
        return task_id

    def test_taskless_eval_shared_run_stays_unknown(self):
        shared = "run-shared-1"
        self._unique_task(1, "codex", shared)
        self._unique_task(2, "opencode", shared)
        eval_store.record_eval_result(
            shared, requirements_satisfied=True, verification_passed=True,
            human_intervention_count=0, final_status="completed",
            task_id=None, workflow_id="wf-hist",
            created_at=BASE_TS + 700.0, db_path=self.db_path,
        )
        rows = adaptive_router.collect_samples(
            self.db_path, node=NODE, task_type=TASK_TYPE, cutoff=CUTOFF,
            agents=["codex", "opencode"])
        by_task = {row["task_id"]: row for row in rows}
        self.assertIsNone(by_task["task-manual-1"]["requirements_satisfied"])
        self.assertIsNone(by_task["task-manual-2"]["requirements_satisfied"])

    def test_taskless_eval_unique_run_is_attributed(self):
        self._unique_task(10, "codex", "run-unique-1")
        eval_store.record_eval_result(
            "run-unique-1", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status="completed", task_id=None, workflow_id="wf-hist",
            created_at=BASE_TS + 700.0, db_path=self.db_path,
        )
        rows = adaptive_router.collect_samples(
            self.db_path, node=NODE, task_type=TASK_TYPE, cutoff=CUTOFF,
            agents=["codex"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["requirements_satisfied"])
        self.assertEqual(rows[0]["final_status"], "completed")

    def test_eval_final_status_never_backfilled_from_task(self):
        self._unique_task(20, "codex", "run-nofinal-1", status="completed")
        eval_store.record_eval_result(
            "run-nofinal-1", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status=None, task_id="task-manual-20",
            workflow_id="wf-hist", created_at=BASE_TS + 700.0,
            db_path=self.db_path,
        )
        rankings = _rank(self.db_path, ["codex"])
        codex = rankings[0]
        self.assertEqual(codex["sample_count"], 0)
        self.assertIsNone(codex["qualified_success_rate"])
        self.assertEqual(
            codex["fallback_reason"], "unknown_outcome_no_determined_samples")

    def test_revision_is_latest_before_cutoff(self):
        self._unique_task(30, "codex", "run-rev-1", status="completed")
        eval_store.record_eval_result(
            "run-rev-1", requirements_satisfied=True,
            verification_passed=True, human_intervention_count=0,
            final_status="completed", task_id="task-manual-30",
            workflow_id="wf-hist", created_at=CUTOFF - 100.0,
            db_path=self.db_path,
        )
        eval_store.record_eval_result(
            "run-rev-1", requirements_satisfied=False,
            verification_passed=False, human_intervention_count=0,
            final_status="failed", task_id="task-manual-30",
            workflow_id="wf-hist", created_at=CUTOFF + 100.0,
            db_path=self.db_path,
        )
        before = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=CUTOFF, active_loads={}, reserved_loads={},
        )
        self.assertAlmostEqual(before[0]["qualified_success_rate"], 1.0)
        after = adaptive_router.rank_candidates(
            ["codex"], db_path=self.db_path, node=NODE, task_type=TASK_TYPE,
            cutoff=CUTOFF + 200.0, active_loads={}, reserved_loads={},
        )
        self.assertAlmostEqual(after[0]["qualified_success_rate"], 0.0)

    def test_slice_query_uses_index_not_full_scan(self):
        import sqlite3

        query, params = state_db._adaptive_slice_query(
            "codex", NODE, TASK_TYPE, CUTOFF, None, None, 500)
        conn = sqlite3.connect(str(self.db_path))
        try:
            plan = " ".join(
                str(row) for row in
                conn.execute("EXPLAIN QUERY PLAN " + query, tuple(params)).fetchall()
            )
        finally:
            conn.close()
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("SCAN t", plan)

    def test_per_agent_windows_not_crowded_out(self):
        for i in range(20):
            _seed_sample(self.store, self.db_path, i, "kimi", success=True)
        for i in range(20, 23):
            _seed_sample(self.store, self.db_path, i, "codex", success=True)
        rankings = adaptive_router.rank_candidates(
            ["kimi", "codex"], db_path=self.db_path, node=NODE,
            task_type=TASK_TYPE, cutoff=CUTOFF, active_loads={},
            reserved_loads={}, limit=5,
        )
        by_agent = {row["agent"]: row for row in rankings}
        self.assertEqual(by_agent["kimi"]["sample_count"], 5)
        self.assertEqual(by_agent["codex"]["sample_count"], 3)
