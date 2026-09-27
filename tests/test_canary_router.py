"""Adaptive Router v2 Canary Mode tests.

Contract under test:
- herdr/canary_router.py: config (default OFF, fail-closed validation),
  deterministic hash split, bucket whitelist, sufficient-only admission
  reusing the authoritative shadow sufficiency functions.
- herdr/agent_router.choose_agent: canary only touches the auto-routing
  branch, diverts to a pool-validated candidate, records exactly one
  route_decision per routing (mode="canary" when the gate admits the
  bucket, mode="shadow" otherwise), and fails open to the legacy pick
  on any canary error.

Protection map (task spec):
1. default off            -> test_default_off_keeps_shadow_behavior
2. whitelist buckets only -> test_*_not_whitelisted_*
3. deterministic split    -> TestCanaryHash + test_plan_divert_follows_hash
4. fail-open to legacy    -> test_canary_fail_open_on_plan_error
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import adaptive_router, agent_router, canary_router, eval_store
from herdr import execution_outcome
from herdr.state_store import get_state_store

BASE_TS = 1_700_000_000.0
CUTOFF = BASE_TS + 100_000.0
NODE = "implementation"
TASK_TYPE = "fix"
WF_ID = "wf-canary"


def _make_env(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-canary-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    env_patch = patch.dict(
        _os_env(), {"HERDR_OUTCOME_AUTOFINALIZE": "0"})
    env_patch.start()
    test.addCleanup(env_patch.stop)
    store = get_state_store(tmp_path / "state.db")
    test.patchers = [
        patch("herdr.agent_router._get_store", return_value=store),
        patch("herdr.agent_router.POOLS_FILE", tmp_path / "agent-pools.json"),
        patch(
            "herdr.agent_router.RESERVATIONS_FILE",
            tmp_path / "agent-reservations.json",
        ),
        patch(
            "herdr.agent_router.ROUTER_LOCK_FILE",
            tmp_path / "agent-router.lock",
        ),
    ]
    for p in test.patchers:
        p.start()
        test.addCleanup(p.stop)
    return store, tmp_path, tmp_path / "state.db"


def _os_env():
    import os as _os
    return _os.environ


def _write_config(tmp_path, payload, name="route-canary.json"):
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _config_env(path):
    return patch.dict(_os_env(), {"HERDR_ROUTE_CANARY_CONFIG": str(path)})


def _seed_sample(store, db_path, idx, agent, *, success=True,
                 verification=True, wall=600.0, rework=False,
                 blocked=False, human=0, node=NODE, task_type=TASK_TYPE,
                 run_prefix="run", ts=None, workflow_id=WF_ID):
    ts = BASE_TS + idx if ts is None else ts
    run_id = f"{run_prefix}-{agent}-{idx}"
    task_id = f"task-{agent}-{idx}"
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
        "status_history": [{"to": s} for s in history],
        "started_at": ts,
        "finished_at": ts + wall,
        "created_at": ts,
    })
    eval_store.record_eval_result(
        run_id,
        requirements_satisfied=bool(success),
        verification_passed=bool(verification),
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


def _seed_decision_history(store, db_path, count, agent, *,
                           node=NODE, task_type=TASK_TYPE, ts_start=None):
    """Seed count settled outcomes + matching frozen route_decision events.

    Each decision's actual_agent == agent and its outcome settles with the
    same identity, so every row calibrates: exactly what shadow sufficiency
    needs to mark a bucket sufficient on both sides.
    """
    ts_start = BASE_TS + 50_000 if ts_start is None else ts_start
    for i in range(count):
        run_id, task_id = _seed_sample(
            store, db_path, i, agent, node=node, task_type=task_type,
            run_prefix="hist")
        rankings = adaptive_router.rank_candidates(
            [agent], db_path=db_path, node=node, task_type=task_type,
            cutoff=ts_start + i + 1.0, active_loads={}, reserved_loads={},
        )
        payload = adaptive_router.build_shadow_decision(
            workflow_id=WF_ID, run_id=run_id, task_id=task_id,
            node=node, task_type=task_type, actual_agent=agent,
            rankings=rankings, created_at=ts_start + i + 2.0,
        )
        store.record_event(
            "route_decision", payload,
            workflow_id=WF_ID, node_id=node, task_id=task_id,
            agent_id=agent, source="adaptive-router-shadow",
            timestamp=ts_start + i + 2.0, run_id=run_id,
        )


def _enable_workflow(store):
    store.save_workflow({
        "workflow_id": WF_ID,
        "project_id": "test-proj",
        "status": "running",
        "healthy_agents": [],
        "unhealthy_agents": {},
    })


def _node_cfg(preferred):
    return {"nodes": [{"id": NODE,
                       "agent_policy": {"preferred": list(preferred)}}]}


class TestCanaryConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-canary-cfg-")
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

    def test_missing_config_is_disabled(self):
        with _config_env(self.tmp_path / "absent.json"):
            config, errors = canary_router.read_canary_config()
        self.assertIsNone(config)
        self.assertEqual(errors, [])

    def test_disabled_flag_is_disabled(self):
        path = _write_config(self.tmp_path, {
            "enabled": False, "percentage": 10,
            "buckets": [{"agent": "codex", "node": NODE,
                         "task_type": TASK_TYPE}],
        })
        with _config_env(path):
            config, errors = canary_router.read_canary_config()
        self.assertIsNone(config)
        self.assertEqual(errors, [])

    def test_malformed_json_is_disabled_with_errors(self):
        path = _write_config(self.tmp_path, "{not json")
        with _config_env(path):
            config, errors = canary_router.read_canary_config()
        self.assertIsNone(config)
        self.assertTrue(errors)

    def test_invalid_percentage_rejected(self):
        for bad in (0, 101, "ten", None):
            path = _write_config(self.tmp_path, {
                "enabled": True, "percentage": bad,
                "buckets": [{"agent": "codex", "node": NODE,
                             "task_type": TASK_TYPE}],
            })
            with _config_env(path):
                config, errors = canary_router.read_canary_config()
            self.assertIsNone(config, f"percentage={bad!r} must fail closed")
            self.assertTrue(errors)

    def test_empty_buckets_rejected(self):
        path = _write_config(self.tmp_path, {
            "enabled": True, "percentage": 10, "buckets": [],
        })
        with _config_env(path):
            config, errors = canary_router.read_canary_config()
        self.assertIsNone(config)
        self.assertTrue(errors)

    def test_valid_config_loads(self):
        path = _write_config(self.tmp_path, {
            "enabled": True, "percentage": 5,
            "buckets": [
                {"agent": "codex", "node": NODE, "task_type": TASK_TYPE,
                 "percentage": 50},
                {"agent": "claude", "node": "test", "task_type": "docs"},
            ],
        })
        with _config_env(path):
            config, errors = canary_router.read_canary_config()
        self.assertEqual(errors, [])
        self.assertIsNotNone(config)
        self.assertEqual(config.percentage, 5)
        self.assertEqual(len(config.buckets), 2)
        self.assertEqual(config.buckets[0].percentage, 50)
        self.assertIsNone(config.buckets[1].percentage)


class TestCanaryHash(unittest.TestCase):
    def test_hash_is_pinned_sha256_mod100(self):
        digest = hashlib.sha256(b"canary-v2|run-1|task-1").digest()
        expected = int.from_bytes(digest[:8], "big") % 100
        self.assertEqual(
            canary_router.canary_hash_bucket("task-1", "run-1"), expected)

    def test_hash_deterministic_across_calls(self):
        first = canary_router.canary_hash_bucket("t-a", "r-a")
        second = canary_router.canary_hash_bucket("t-a", "r-a")
        self.assertEqual(first, second)

    def test_hash_requires_task_and_run(self):
        self.assertIsNone(canary_router.canary_hash_bucket("", "run-1"))
        self.assertIsNone(canary_router.canary_hash_bucket("task-1", ""))
        self.assertIsNone(canary_router.canary_hash_bucket(None, None))

    def test_hash_bucket_in_range(self):
        for i in range(200):
            bucket = canary_router.canary_hash_bucket(f"task-{i}", f"run-{i}")
            self.assertIsNotNone(bucket)
            self.assertGreaterEqual(bucket, 0)
            self.assertLess(bucket, 100)


class TestCanaryPlan(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp_path, self.db_path = _make_env(self)
        self.config = canary_router.CanaryConfig(
            percentage=100,
            buckets=(canary_router.CanaryBucket(
                agent="codex", node=NODE, task_type=TASK_TYPE),),
            admission_scan_cap=500,
        )

    def _plan(self, candidates=("opencode", "codex"), task_id="task-cur",
              run_id="run-cur", config=None):
        return canary_router.plan_canary(
            config=config or self.config,
            candidates=list(candidates),
            node=NODE,
            task_type=TASK_TYPE,
            task_id=task_id,
            run_id=run_id,
            db_path=self.db_path,
            decided_at=CUTOFF,
            active_loads={},
            reserved_loads={},
            exclude_run_id=run_id,
        )

    def test_plan_none_without_history(self):
        self.assertIsNone(self._plan())

    def test_plan_none_when_bucket_not_whitelisted(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        config = canary_router.CanaryConfig(
            percentage=100,
            buckets=(canary_router.CanaryBucket(
                agent="claude", node=NODE, task_type=TASK_TYPE),),
            admission_scan_cap=500,
        )
        # codex wins the ranking but only claude is whitelisted.
        self.assertIsNone(self._plan(config=config))

    def test_plan_none_when_admission_warming(self):
        _seed_decision_history(self.store, self.db_path, 10, "codex")
        self.assertIsNone(self._plan())

    def test_plan_admitted_when_sufficient(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        plan = self._plan()
        self.assertIsNotNone(plan)
        self.assertEqual(plan.recommended, "codex")
        self.assertEqual(plan.gate["bucket_key"], f"codex/{NODE}/{TASK_TYPE}")
        admission = plan.gate["admission"]
        self.assertEqual(admission["model_data_status"], "sufficient")
        self.assertEqual(admission["evaluation_data_status"], "sufficient")
        self.assertFalse(plan.gate["truncated"])

    def test_plan_divert_follows_hash(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        # percentage=100: every hash bucket diverts.
        plan = self._plan()
        self.assertTrue(plan.hash_divert)
        # percentage=1: only hash bucket 0 diverts; pick a task outside it.
        strict = canary_router.CanaryConfig(
            percentage=1, buckets=self.config.buckets,
            admission_scan_cap=500,
        )
        diverted_any = False
        for i in range(50):
            plan_i = self._plan(
                task_id=f"task-p-{i}", run_id=f"run-p-{i}", config=strict)
            bucket = canary_router.canary_hash_bucket(
                f"task-p-{i}", f"run-p-{i}")
            self.assertIsNotNone(plan_i)
            self.assertEqual(plan_i.hash_divert, bucket == 0)
            diverted_any = diverted_any or plan_i.hash_divert
        self.assertTrue(diverted_any, "1% of 50 deterministic ids must hit")

    def test_plan_per_bucket_percentage_override(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        override = canary_router.CanaryConfig(
            percentage=1,
            buckets=(canary_router.CanaryBucket(
                agent="codex", node=NODE, task_type=TASK_TYPE,
                percentage=100),),
            admission_scan_cap=500,
        )
        plan = self._plan(config=override)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.hash_divert)
        self.assertEqual(plan.gate["effective_percentage"], 100)


class CanaryRoutingTestBase(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp_path, self.db_path = _make_env(self)
        _enable_workflow(self.store)

    def _enable_canary(self, percentage=100, agent="codex", node=NODE,
                       task_type=TASK_TYPE):
        path = _write_config(self.tmp_path, {
            "enabled": True, "percentage": percentage,
            "buckets": [{"agent": agent, "node": node,
                         "task_type": task_type}],
        })
        patcher = _config_env(path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def _route(self, task_id, run_id, preferred=("opencode", "codex"),
               requested="auto"):
        with patch("herdr.agent_router.workflow_config_for",
                   return_value=_node_cfg(preferred)):
            return agent_router.choose_agent(
                WF_ID, NODE, TASK_TYPE, requested=requested,
                reservation_key=task_id, run_id=run_id,
            )

    def _decisions(self):
        return self.store.list_events(event_type="route_decision")

    def _reservation(self, task_id):
        data = json.loads(
            Path(agent_router.RESERVATIONS_FILE).read_text(encoding="utf-8"))
        return data.get("reservations", {}).get(task_id)


class TestCanaryRouting(CanaryRoutingTestBase):
    def test_default_off_keeps_shadow_behavior(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        selected = self._route("task-off", "run-off")
        self.assertEqual(selected, "opencode")
        decisions = self._decisions()
        canary = [e for e in decisions
                  if e["payload"].get("mode") == "canary"]
        self.assertEqual(canary, [])
        shadow = [e for e in decisions
                  if e["payload"].get("mode") == "shadow"]
        self.assertTrue(shadow)
        self.assertEqual(shadow[-1]["payload"]["actual_agent"], "opencode")

    def test_canary_diverts_to_recommended(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=100)
        selected = self._route("task-div", "run-div")
        self.assertEqual(selected, "codex")
        decisions = self._decisions()
        canary = [e for e in decisions
                  if e["payload"].get("mode") == "canary"]
        self.assertEqual(len(canary), 1, "exactly one decision per routing")
        payload = canary[0]["payload"]
        self.assertEqual(payload["actual_agent"], "codex")
        self.assertEqual(payload["recommended_agent"], "codex")
        self.assertEqual(payload["legacy_agent"], "opencode")
        self.assertTrue(payload["diverted"])
        self.assertEqual(
            payload["algorithm_version"],
            canary_router.CANARY_ALGORITHM_VERSION)
        self.assertIn("candidate_rankings", payload)
        self.assertIn("canary_gate", payload)
        # Reservation must match the dispatched (diverted) agent.
        reservation = self._reservation("task-div")
        self.assertEqual(reservation["agent"], "codex")

    def test_hash_fail_stays_legacy_but_records_canary_decision(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=1)
        legacy_case = None
        for i in range(50):
            task_id, run_id = f"task-hf-{i}", f"run-hf-{i}"
            selected = self._route(task_id, run_id)
            bucket = canary_router.canary_hash_bucket(task_id, run_id)
            if bucket == 0:
                # The single 1% slot: diverts to the recommendation.
                self.assertEqual(selected, "codex")
                continue
            self.assertEqual(selected, "opencode")
            legacy_case = (task_id, selected)
            break
        self.assertIsNotNone(legacy_case, "some id must fall outside the 1%")
        task_id, selected = legacy_case
        payload = self._decisions()[-1]["payload"]
        self.assertEqual(payload["mode"], "canary")
        self.assertFalse(payload["diverted"])
        self.assertEqual(payload["actual_agent"], "opencode")
        self.assertEqual(payload["legacy_agent"], "opencode")
        self.assertEqual(self._reservation(task_id)["agent"], "opencode")

    def test_same_agent_agreement_records_not_diverted(self):
        _seed_decision_history(self.store, self.db_path, 30, "opencode")
        self._enable_canary(percentage=100, agent="opencode")
        selected = self._route("task-same", "run-same")
        self.assertEqual(selected, "opencode")
        payload = self._decisions()[-1]["payload"]
        self.assertEqual(payload["mode"], "canary")
        self.assertFalse(payload["diverted"])
        self.assertEqual(payload["recommended_agent"], "opencode")
        self.assertEqual(payload["legacy_agent"], "opencode")
        self.assertTrue(payload["canary_gate"]["would_divert"] is False)

    def test_not_whitelisted_task_type_records_shadow(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=100, task_type="docs")
        selected = self._route("task-nw", "run-nw")
        self.assertEqual(selected, "opencode")
        payload = self._decisions()[-1]["payload"]
        self.assertEqual(payload["mode"], "shadow")

    def test_explicit_request_bypasses_canary(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=100)
        selected = self._route("task-exp", "run-exp", requested="claude")
        self.assertEqual(selected, "claude")
        canary = [e for e in self._decisions()
                  if e["payload"].get("mode") == "canary"]
        self.assertEqual(canary, [])

    def test_canary_fail_open_on_plan_error(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=100)
        with patch("herdr.canary_router.plan_canary",
                   side_effect=RuntimeError("canary-boom")):
            selected = self._route("task-fo", "run-fo")
        self.assertEqual(selected, "opencode")
        errors = self.store.list_events(event_type="route_decision_error")
        canary_errors = [e for e in errors
                         if e["payload"].get("mode") == "canary"]
        self.assertTrue(canary_errors, "fail-open must leave an audit event")
        self.assertIn("canary-boom",
                      str(canary_errors[-1]["payload"].get("error")))

    def test_canary_never_recommends_isolation_excluded_agent(self):
        # opencode was used in the implementation stage; the "test" stage
        # excludes implementation agents by default (fail-closed FR-6.1).
        # Even with codex whitelisted for the test stage, codex is not in
        # the test-stage candidate pool, so no diversion can happen to it.
        self.store.save_task({
            "task_id": "task-impl-used",
            "workflow_id": WF_ID,
            "run_id": "run-impl-used",
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })
        self._enable_canary(percentage=100, agent="codex", node="test",
                            task_type=TASK_TYPE)
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value={"nodes": [{
                "id": "test",
                "agent_policy": {"preferred": ["opencode", "codex"]},
            }]},
        ):
            selected = agent_router.choose_agent(
                WF_ID, "test", TASK_TYPE, requested="auto",
                reservation_key="task-iso", run_id="run-iso",
            )
        self.assertEqual(selected, "opencode")

    def test_agent_override_bypasses_canary(self):
        _seed_decision_history(self.store, self.db_path, 30, "codex")
        self._enable_canary(percentage=100)
        self.store.save_workflow({
            "workflow_id": WF_ID,
            "project_id": "test-proj",
            "status": "running",
            "agent_override": "claude",
        })
        with patch("herdr.agent_router.workflow_config_for",
                   return_value=_node_cfg(["opencode", "codex"])):
            selected = agent_router.choose_agent(
                WF_ID, NODE, TASK_TYPE, requested="auto",
                reservation_key="task-ovr", run_id="run-ovr",
            )
        self.assertEqual(selected, "claude")
        canary = [e for e in self._decisions()
                  if e["payload"].get("mode") == "canary"]
        self.assertEqual(canary, [])


class TestCanaryDecisionPayload(unittest.TestCase):
    def test_canary_decision_payload_shape(self):
        rankings = [{
            "agent": "codex", "rank": 1, "sample_count": 30,
            "confidence": 0.9, "qualified_success_rate": 0.9,
            "blended_success_rate": 0.9, "etqs_seconds": 600.0,
            "p50_wall_time_seconds": 600.0,
        }]
        gate = {
            "bucket_key": f"codex/{NODE}/{TASK_TYPE}",
            "hash_bucket": 42,
            "effective_percentage": 100,
            "hash_divert": True,
            "would_divert": True,
            "truncated": False,
            "admission": {
                "model_data_status": "sufficient",
                "evaluation_data_status": "sufficient",
                "model_sample_count": 30,
                "evaluation_sample_count": 30,
            },
        }
        decision = adaptive_router.build_canary_decision(
            workflow_id=WF_ID, run_id="run-1", task_id="task-1",
            node=NODE, task_type=TASK_TYPE, actual_agent="codex",
            recommended_agent="codex", legacy_agent="opencode",
            diverted=True, rankings=rankings, gate=gate,
            created_at=CUTOFF,
        )
        self.assertEqual(decision["mode"], "canary")
        self.assertEqual(decision["actual_agent"], "codex")
        self.assertEqual(decision["recommended_agent"], "codex")
        self.assertEqual(decision["legacy_agent"], "opencode")
        self.assertTrue(decision["diverted"])
        self.assertTrue(decision["same_decision"])
        self.assertEqual(
            decision["algorithm_version"],
            canary_router.CANARY_ALGORITHM_VERSION)
        self.assertEqual(decision["canary_gate"]["hash_bucket"], 42)
        self.assertEqual(decision["candidate_rankings"], rankings)
        self.assertEqual(decision["created_at"], CUTOFF)
        # Payload must be JSON-serializable for the event store.
        json.dumps(decision)

    def test_diverted_disagreement_marks_same_decision_false(self):
        decision = adaptive_router.build_canary_decision(
            workflow_id=WF_ID, run_id="run-2", task_id="task-2",
            node=NODE, task_type=TASK_TYPE, actual_agent="codex",
            recommended_agent="claude", legacy_agent="opencode",
            diverted=True, rankings=[], gate={},
            created_at=CUTOFF,
        )
        self.assertFalse(decision["same_decision"])


if __name__ == "__main__":
    unittest.main()
