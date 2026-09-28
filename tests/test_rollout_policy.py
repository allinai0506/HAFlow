"""Adaptive Router Controlled Rollout tests (PR #104 concept).

Contract under test:
- herdr/rollout_policy.py: staged enum off/5/10/25/50, manual promote only,
  no auto-promotion, adjacent-only promote, direct rollback to off,
  atomic state + immutable audit, bucket isolation, hash monotonicity
  (via canary_router.canary_hash_bucket), fail-safe (invalid/DB/kill ->
  never expands), kill switch, safety guard consuming canary_evaluation
  facts only.

Protection map (task spec sections):
- default safe / kill -> Legacy
- manual promote off->5->10->25->50
- no skip (5->50 rejected)
- emergency rollback 50->off
- bucket isolation
- hash stable monotonic 5 subset 10 subset 25 subset 50
- audit exactly one per change
- invalid config never expands
- DB failure atomic (no partial)
- auto rollback on guard -> off + auto_rollback audit
- no auto promotion even on great canary data
- CLI entry (bin/herdr-task rollout status/set/off/history/check-guard)
"""

import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from herdr import canary_router
from herdr import rollout_policy
from herdr import state_db
from herdr.state_store import get_state_store


def _make_store(test):
    tmp = tempfile.TemporaryDirectory(prefix="herdr-rollout-")
    test.addCleanup(tmp.cleanup)
    tmp_path = Path(tmp.name)
    env_patch = patch.dict(
        "os.environ",
        {
            "HERDR_OUTCOME_AUTOFINALIZE": "0",
            "HERDR_ADAPTIVE_ROLLOUT_ENABLED": "true",
            "HERDR_ROLLOUT_GUARD_ENABLED": "1",
        },
    )
    env_patch.start()
    test.addCleanup(env_patch.stop)
    store = get_state_store(tmp_path / "state.db")
    return store, tmp_path / "state.db"


def _seed_collapsed_bucket(store, db_path, *, test):
    """A canary bucket whose adaptive arm collapsed (guard triggers)."""
    from herdr import adaptive_router, eval_store, execution_outcome
    base = 1_700_000_000.0
    for index in range(12):
        for agent, diverted, success in (
            ("codex", True, index >= 12),     # adaptive: all failures
            ("opencode", False, True),        # legacy: all successes
        ):
            run_id = f"run-coll-{agent}-{index}"
            task_id = f"task-coll-{agent}-{index}"
            ts = base + index
            status = "completed" if success else "failed"
            store.save_task({
                "task_id": task_id, "workflow_id": "wf-coll",
                "run_id": run_id, "node": "implementation",
                "stage": "implementation", "task_type": "fix",
                "agent": agent, "status": status,
                "status_history": [{"to": s} for s in (
                    "pending", "dispatched", "working", "agent_done",
                    status)],
                "started_at": ts, "finished_at": ts + 100.0,
                "created_at": ts,
            })
            eval_store.record_eval_result(
                run_id, requirements_satisfied=bool(success),
                verification_passed=True, human_intervention_count=0,
                final_status=status, task_id=task_id,
                workflow_id="wf-coll", created_at=ts + 101.0,
                db_path=db_path)
            settled = execution_outcome.finalize_execution_outcome(
                task_id, db_path=db_path, finalized_at=ts + 105.0)
            test.assertEqual(settled["status"], "created")
            payload = adaptive_router.build_canary_decision(
                workflow_id="wf-coll", run_id=run_id, task_id=task_id,
                node="implementation", task_type="fix",
                actual_agent=agent, recommended_agent="codex",
                legacy_agent="opencode", diverted=diverted, rankings=[],
                gate={}, created_at=ts + 200.0)
            store.record_event(
                "route_decision", payload, workflow_id="wf-coll",
                node_id="implementation", task_id=task_id,
                agent_id=agent, source="adaptive-router-canary",
                timestamp=ts + 200.0, run_id=run_id)


def _seed_episode_samples(test, store, db_path, *, start_ts, tag,
                          adaptive_successes, adaptive_failures,
                          legacy_successes, legacy_failures):
    """Seed settled canary decisions for codex/implementation/fix.

    Same shape as ``TestGuard._seed_canary_bucket`` but with explicit
    timestamps: the caller controls which rollout episode the samples
    belong to. Adaptive arm = diverted codex executions; legacy arm =
    undiverted opencode. Decision events land at ``start_ts + i +
    200``; outcomes at ``start_ts + i + 105``.
    """
    from herdr import adaptive_router, eval_store, execution_outcome
    index = 0

    def settle(agent, success, run_id, task_id, ts):
        store.save_task({
            "task_id": task_id, "workflow_id": "wf-ep",
            "run_id": run_id, "node": "implementation",
            "stage": "implementation", "task_type": "fix",
            "agent": agent, "status": "completed" if success else "failed",
            "status_history": [{"to": s} for s in (
                "pending", "dispatched", "working", "agent_done",
                "completed" if success else "failed")],
            "started_at": ts, "finished_at": ts + 100.0,
            "created_at": ts,
        })
        eval_store.record_eval_result(
            run_id, requirements_satisfied=bool(success),
            verification_passed=True, human_intervention_count=0,
            final_status="completed" if success else "failed",
            task_id=task_id, workflow_id="wf-ep",
            created_at=ts + 101.0, db_path=db_path)
        out = execution_outcome.finalize_execution_outcome(
            task_id, db_path=db_path, finalized_at=ts + 105.0)
        test.assertEqual(out["status"], "created")

    def decide(run_id, task_id, actual, diverted, ts):
        payload = adaptive_router.build_canary_decision(
            workflow_id="wf-ep", run_id=run_id, task_id=task_id,
            node="implementation", task_type="fix", actual_agent=actual,
            recommended_agent="codex", legacy_agent="opencode",
            diverted=diverted, rankings=[], gate={}, created_at=ts)
        store.record_event(
            "route_decision", payload, workflow_id="wf-ep",
            node_id="implementation", task_id=task_id, agent_id=actual,
            source="adaptive-router-canary", timestamp=ts, run_id=run_id)

    for i in range(adaptive_successes + adaptive_failures):
        ts = start_ts + index
        run_id, task_id = f"run-{tag}-a{index}", f"task-{tag}-a{index}"
        settle("codex", i < adaptive_successes, run_id, task_id, ts)
        decide(run_id, task_id, "codex", True, ts + 200.0)
        index += 1
    for i in range(legacy_successes + legacy_failures):
        ts = start_ts + index
        run_id, task_id = f"run-{tag}-l{index}", f"task-{tag}-l{index}"
        settle("opencode", i < legacy_successes, run_id, task_id, ts)
        decide(run_id, task_id, "opencode", False, ts + 200.0)
        index += 1


class TestStages(unittest.TestCase):
    def test_allowed_stages_exact(self):
        self.assertEqual(
            rollout_policy.ALLOWED_PERCENTAGES, (0, 5, 10, 25, 50))

    def test_normalize_off(self):
        self.assertEqual(rollout_policy.normalize_percentage("off"), 0)
        self.assertEqual(rollout_policy.normalize_percentage(0), 0)
        self.assertEqual(rollout_policy.normalize_percentage(None), 0)

    def test_normalize_rejects_open_int(self):
        for bad in (75, 100, 1, 7, -5, "10%"):
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                rollout_policy.normalize_percentage(bad)

    def test_adjacent_promote_allowed(self):
        self.assertTrue(rollout_policy.is_valid_transition(0, 5))
        self.assertTrue(rollout_policy.is_valid_transition(5, 10))
        self.assertTrue(rollout_policy.is_valid_transition(10, 25))
        self.assertTrue(rollout_policy.is_valid_transition(25, 50))

    def test_skip_rejected(self):
        self.assertFalse(rollout_policy.is_valid_transition(5, 50))
        self.assertFalse(rollout_policy.is_valid_transition(0, 10))
        self.assertFalse(rollout_policy.is_valid_transition(10, 50))
        self.assertFalse(rollout_policy.is_valid_transition(0, 50))

    def test_emergency_rollback_always_allowed(self):
        for cur in (5, 10, 25, 50):
            self.assertTrue(
                rollout_policy.is_valid_transition(cur, 0),
                f"{cur} -> off must be allowed")


class TestManualPromote(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def test_full_ladder(self):
        for prev, nxt in ((0, 5), (5, 10), (10, 25), (25, 50)):
            res = rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=nxt,
                reason=f"promote {prev}->{nxt}", source="cli:test")
            self.assertEqual(res["previous_percentage"], prev)
            self.assertEqual(res["new_percentage"], nxt)
            self.assertEqual(res["action"], "promote")
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 50)

    def test_skip_refused(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="init", source="cli")
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=50, reason="skip",
                source="cli")
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 5)

    def test_emergency_rollback(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="init", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="p", source="cli")
        res = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="manual rollback",
            source="cli")
        self.assertEqual(res["action"], "rollback")
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 0)

    def test_reason_required(self):
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=5, reason="  ",
                source="cli")

    def test_bucket_identity_invalid(self):
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="", node="implementation",
                task_type="fix", new_percentage=5, reason="r",
                source="cli")

    def test_bucket_isolation(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "claude", "implementation", "fix"), 0)
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "test", "fix"), 0)
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "feat"), 0)

    def test_audit_one_per_change(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r1", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="r2", source="cli")
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 2)
        # Newest-first: latest change first.
        self.assertEqual(history[0]["previous_percentage"], 5)
        self.assertEqual(history[0]["new_percentage"], 10)
        self.assertEqual(history[1]["previous_percentage"], 0)
        self.assertEqual(history[1]["new_percentage"], 5)
        for entry in history:
            for key in ("recommended_agent", "node", "task_type",
                        "previous_percentage", "new_percentage", "action",
                        "reason", "source", "algorithm_version",
                        "created_at"):
                self.assertIn(key, entry)


class TestHashMonotonic(unittest.TestCase):
    def test_subset_chain(self):
        ids = [(f"task-{i}", f"run-{i}") for i in range(300)]
        for task_id, run_id in ids:
            bucket = canary_router.canary_hash_bucket(task_id, run_id)
            self.assertIsNotNone(bucket)
            in5 = bucket < 5
            in10 = bucket < 10
            in25 = bucket < 25
            in50 = bucket < 50
            # Monotonic inclusion: 5% subset 10% subset 25% subset 50%.
            if in5:
                self.assertTrue(in10 and in25 and in50)
            if in10:
                self.assertTrue(in25 and in50)
            if in25:
                self.assertTrue(in50)

    def test_effective_percentage_drives_same_hash(self):
        # Same identity, different stages: membership only grows.
        task_id, run_id = "task-stable-1", "run-stable-1"
        bucket = canary_router.canary_hash_bucket(task_id, run_id)
        seen = [p for p in (5, 10, 25, 50) if bucket < p]
        # Either empty or a suffix of the ladder (no re-shuffle).
        if seen:
            self.assertEqual(
                seen, [p for p in (5, 10, 25, 50) if p >= seen[0]])


class TestFailSafe(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def test_kill_switch_forces_zero(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="r", source="cli")
        with patch.dict("os.environ",
                        {"HERDR_ADAPTIVE_ROLLOUT_ENABLED": "false"}):
            self.assertEqual(
                rollout_policy.effective_percentage(
                    self.db_path, agent="codex", node="implementation",
                    task_type="fix", config_fallback=100), 0)

    def test_invalid_bucket_forces_zero(self):
        self.assertEqual(
            rollout_policy.effective_percentage(
                self.db_path, agent="", node="implementation",
                task_type="fix", config_fallback=50), 0)

    def test_db_failure_forces_zero_never_expands(self):
        # A directory is not a SQLite file: reads must fail safe to 0
        # instead of raising or returning the config fallback.
        value = rollout_policy.effective_percentage(
            self.db_path.parent, agent="codex", node="implementation",
            task_type="fix", config_fallback=50)
        self.assertEqual(value, 0)

    def test_no_row_falls_back_to_config(self):
        # Not-yet-migrated bucket preserves #103 behavior.
        self.assertEqual(
            rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=5), 5)

    def test_staged_overrides_config(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="r", source="cli")
        self.assertEqual(
            rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=100), 10)

    def test_unknown_percentage_never_parsed(self):
        with self.assertRaises(ValueError):
            rollout_policy.normalize_percentage(100)

    def test_explicit_off_overrides_config_fallback(self):
        # Reviewer P1: absent is NOT explicit off. A config-only bucket
        # running at 50% must not report "off" as a no-op.
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="kill it",
            source="cli", config_fallback=50)
        self.assertEqual(record["action"], "rollback")
        self.assertEqual(record["changed"], True)
        # Explicit state exists and wins over the config fallback forever.
        self.assertTrue(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 0)
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=50), 0)
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous_percentage"], 50)
        self.assertEqual(history[0]["new_percentage"], 0)
        self.assertEqual(history[0]["action"], "rollback")

    def test_explicit_off_on_non_serving_bucket_is_noop(self):
        # No staged row AND no canary config traffic: nothing is being
        # diverted, so there is genuinely nothing to change.
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="already off",
            source="cli", config_fallback=None)
        self.assertEqual(record["action"], "noop")
        self.assertFalse(record["changed"])
        self.assertFalse(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))

    def test_repeat_off_after_explicit_off_is_noop(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="kill it",
            source="cli", config_fallback=50)
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="again",
            source="cli", config_fallback=50)
        self.assertEqual(record["action"], "noop")
        self.assertEqual(len(rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")), 1)

    def test_first_migration_audit_records_real_traffic(self):
        # A config-only bucket at 50% migrated to stage 5: the audit must
        # record the traffic that was actually being diverted, and the
        # action must agree with it. 50% -> 5% is a reduction.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="take over",
            source="cli", config_fallback=50)
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(history[0]["previous_percentage"], 50)
        self.assertEqual(history[0]["new_percentage"], 5)
        self.assertEqual(history[0]["action"], "rollback")
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=50), 5)

    def test_migration_from_full_config_is_a_rollback(self):
        # 100% -> 5%: promoting would be an outright lie in the audit.
        rollout_policy.set_stage(
            self.db_path, agent="claude", node="test", task_type="docs",
            new_percentage=5, reason="take over from full canary",
            source="cli", config_fallback=100)
        history = rollout_policy.get_history(
            self.db_path, agent="claude", node="test", task_type="docs")
        self.assertEqual(history[0]["previous_percentage"], 100)
        self.assertEqual(history[0]["new_percentage"], 5)
        self.assertEqual(history[0]["action"], "rollback")

    def test_audit_action_matches_traffic_direction(self):
        # The action contract as an explicit table. An audit row must
        # never say "promote" while recording a smaller number.
        # (config fallback, walk ladder to, target, expected previous,
        #  expected action)
        #
        # Note the classic 5 -> 10 case needs no config fallback: with
        # fallback=5 the walk to stage 5 is a *takeover* (traffic is
        # already 5%, but ownership moves to an explicit staged row), so
        # jumping to 10 without it stays a genuine cross-stage rejection.
        # The ladder is validated against the staged value, which is 0
        # until a row is written.
        cases = [
            (None, 0, 5, 0, "promote"),     # 0 -> 5
            (None, 5, 10, 5, "promote"),    # 5 -> 10
            (50, 0, 5, 50, "rollback"),      # 50 -> 5
            (100, 0, 5, 100, "rollback"),   # 100 -> 5
            (50, 0, 0, 50, "rollback"),      # 50 -> 0
            (10, 10, 0, 10, "rollback"),     # 10 -> 0
        ]
        ladder = [5, 10, 25, 50]
        for index, case in enumerate(cases):
            fallback, staged, target, expected_prev, expected = case
            with self.subTest(fallback=fallback, staged=staged,
                              target=target):
                agent = f"agent-{index}"
                db = self.db_path.parent / f"action-{index}.db"
                for step in [s for s in ladder if s <= staged]:
                    rollout_policy.set_stage(
                        db, agent=agent, node="n", task_type="t",
                        new_percentage=step, reason="ladder", source="cli",
                        config_fallback=fallback)
                rollout_policy.set_stage(
                    db, agent=agent, node="n", task_type="t",
                    new_percentage=target, reason="step", source="cli",
                    config_fallback=fallback)
                record = rollout_policy.get_history(
                    db, agent=agent, node="n", task_type="t")[0]
                self.assertEqual(record["previous_percentage"],
                                 expected_prev)
                self.assertEqual(record["new_percentage"], target)
                self.assertEqual(record["action"], expected)
                # The invariant the reviewer pointed at: the label and
                # the recorded direction can never disagree.
                grew = target > expected_prev
                self.assertEqual(record["action"] == "promote", grew)

    def test_guard_outage_forces_legacy_on_hot_path(self):
        # Reviewer P1: an evaluation that cannot run must never read as
        # "no problem" once the hot guard is explicitly enabled.
        with (patch.dict("os.environ", {"HERDR_ROLLOUT_HOT_GUARD": "1"}),
                patch.object(
                    rollout_policy, "_bucket_report",
                    side_effect=RuntimeError("evaluation db down"))):
            verdict = rollout_policy.evaluate_guard(
                self.db_path, agent="codex", node="implementation",
                task_type="fix")
            self.assertFalse(verdict["triggered"])
            self.assertEqual(verdict["status"], "unavailable")
            self.assertTrue(rollout_policy.should_force_legacy(
                self.db_path, agent="codex", node="implementation",
                task_type="fix"))

    def test_guard_insufficient_samples_still_allows_adaptive(self):
        # Sample starvation is a decision, not an outage.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        with patch.dict("os.environ", {"HERDR_ROLLOUT_HOT_GUARD": "1"}):
            self.assertFalse(rollout_policy.should_force_legacy(
                self.db_path, agent="codex", node="implementation",
                task_type="fix"))

    def test_guard_disabled_keeps_adaptive_traffic(self):
        # Operator opting out of the guard is authoritative.
        with patch.dict("os.environ", {"HERDR_ROLLOUT_HOT_GUARD": "1",
                                      "HERDR_ROLLOUT_GUARD_ENABLED": "0"}):
            self.assertFalse(rollout_policy.should_force_legacy(
                self.db_path, agent="codex", node="implementation",
                task_type="fix"))

    def test_auto_rollback_suppresses_config_only_traffic(self):
        # Reviewer P1: the guard must not report "already off" for a
        # config-only bucket that is still diverting.
        _seed_collapsed_bucket(self.store, self.db_path, test=self)
        result = rollout_policy.maybe_auto_rollback(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", reason="guard", config_fallback=50)
        self.assertEqual(result["action"], "auto_rollback")
        self.assertTrue(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=50), 0)

    def test_default_is_closed(self):
        # No env, no staged row, no canary config: rollout resolves to 0,
        # so a fresh install expands no Adaptive traffic.
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(rollout_policy.rollout_enabled())
            self.assertEqual(
                rollout_policy.effective_percentage(
                    self.db_path, agent="codex", node="implementation",
                    task_type="fix"), 0)
            # Explicitly killed behaves the same.
        with patch.dict("os.environ",
                        {"HERDR_ADAPTIVE_ROLLOUT_ENABLED": "false"}):
            self.assertFalse(rollout_policy.rollout_enabled())

class TestNoAutoPromotion(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def test_great_canary_data_does_not_promote(self):
        # The reverse test the spec demands: perfect adaptive data must
        # never move a stage on its own. Only the guard path may write,
        # and it may only write "down".
        from herdr import adaptive_router, canary_evaluation, eval_store
        from herdr import execution_outcome
        base = 1_700_000_000.0
        for index in range(12):
            for agent, diverted in (("codex", True), ("opencode", False)):
                run_id = f"run-best-{agent}-{index}"
                task_id = f"task-best-{agent}-{index}"
                ts = base + index
                self.store.save_task({
                    "task_id": task_id, "workflow_id": "wf-best",
                    "run_id": run_id, "node": "implementation",
                    "stage": "implementation", "task_type": "fix",
                    "agent": agent, "status": "completed",
                    "status_history": [{"to": s} for s in (
                        "pending", "dispatched", "working", "agent_done",
                        "completed")],
                    "started_at": ts, "finished_at": ts + 100.0,
                    "created_at": ts,
                })
                eval_store.record_eval_result(
                    run_id, requirements_satisfied=True,
                    verification_passed=True, human_intervention_count=0,
                    final_status="completed", task_id=task_id,
                    workflow_id="wf-best", created_at=ts + 101.0,
                    db_path=self.db_path)
                settled = execution_outcome.finalize_execution_outcome(
                    task_id, db_path=self.db_path, finalized_at=ts + 105.0)
                self.assertEqual(settled["status"], "created")
                payload = adaptive_router.build_canary_decision(
                    workflow_id="wf-best", run_id=run_id, task_id=task_id,
                    node="implementation", task_type="fix",
                    actual_agent=agent, recommended_agent="codex",
                    legacy_agent="opencode", diverted=diverted, rankings=[],
                    gate={}, created_at=ts + 200.0)
                self.store.record_event(
                    "route_decision", payload, workflow_id="wf-best",
                    node_id="implementation", task_id=task_id,
                    agent_id=agent, source="adaptive-router-canary",
                    timestamp=ts + 200.0, run_id=run_id)

        # Empty report keeps the trivial invariant.
        self.assertEqual(
            canary_evaluation.build_canary_evaluation_report([])["buckets"],
            [])

        # Pin the activation before the seeded samples (base
        # 1_700_000_000): evidence is episode-scoped, and this test's
        # point is that even perfect *visible* data never promotes.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="manual",
            source="cli", created_at=1_699_999_900.0)
        before = rollout_policy.get_stage(
            self.db_path, "codex", "implementation", "fix")
        history_before = len(rollout_policy.get_history(self.db_path))

        verdict = rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertFalse(verdict["triggered"])
        result = rollout_policy.maybe_auto_rollback(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", reason="best data")
        self.assertEqual(result["action"], "none")

        self.assertEqual(rollout_policy.get_stage(
            self.db_path, "codex", "implementation", "fix"), before)
        self.assertEqual(
            len(rollout_policy.get_history(self.db_path)), history_before)
        # Newest-first: the only recorded transition is the manual one.
        self.assertEqual(
            [row["new_percentage"] for row in
             rollout_policy.get_history(self.db_path)], [5])


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def _seed_canary_bucket(self, adaptive_success, legacy_success,
                            adaptive_n=12, legacy_n=12,
                            recommended="codex"):
        from herdr import adaptive_router, eval_store, execution_outcome
        base = 1_700_000_000.0
        idx = 0

        def settle(agent, success, run_id, task_id, ts):
            self.store.save_task({
                "task_id": task_id, "workflow_id": "wf-guard",
                "run_id": run_id, "node": "implementation",
                "stage": "implementation", "task_type": "fix",
                "agent": agent, "status": "completed" if success else "failed",
                "status_history": [{"to": s} for s in (
                    "pending", "dispatched", "working", "agent_done",
                    "completed" if success else "failed")],
                "started_at": ts, "finished_at": ts + 100.0,
                "created_at": ts,
            })
            eval_store.record_eval_result(
                run_id, requirements_satisfied=bool(success),
                verification_passed=True, human_intervention_count=0,
                final_status="completed" if success else "failed",
                task_id=task_id, workflow_id="wf-guard",
                created_at=ts + 101.0, db_path=self.db_path)
            out = execution_outcome.finalize_execution_outcome(
                task_id, db_path=self.db_path, finalized_at=ts + 105.0)
            assert out["status"] == "created", out

        def decide(run_id, task_id, actual, diverted, ts):
            payload = adaptive_router.build_canary_decision(
                workflow_id="wf-guard", run_id=run_id, task_id=task_id,
                node="implementation", task_type="fix", actual_agent=actual,
                recommended_agent=recommended, legacy_agent="opencode",
                diverted=diverted, rankings=[], gate={}, created_at=ts)
            self.store.record_event(
                "route_decision", payload, workflow_id="wf-guard",
                node_id="implementation", task_id=task_id, agent_id=actual,
                source="adaptive-router-canary", timestamp=ts, run_id=run_id)

        for i in range(adaptive_n):
            ok = i < int(adaptive_n * adaptive_success)
            run_id, task_id = f"run-a-{idx}", f"task-a-{idx}"
            ts = base + idx
            settle(recommended, ok, run_id, task_id, ts)
            decide(run_id, task_id, recommended, True, ts + 200.0)
            idx += 1
        for i in range(legacy_n):
            ok = i < int(legacy_n * legacy_success)
            run_id, task_id = f"run-l-{idx}", f"task-l-{idx}"
            ts = base + idx
            settle("opencode", ok, run_id, task_id, ts)
            decide(run_id, task_id, "opencode", False, ts + 200.0)
            idx += 1

    def test_guard_triggers_on_collapsed_adaptive(self):
        # Guard evidence is episode-scoped: only decisions at or after
        # the bucket's newest staged-row change count. Pin the stage
        # changes just before the seeded samples (base 1_700_000_000)
        # so they belong to the current episode.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli",
            created_at=1_699_999_900.0)
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="r", source="cli",
            created_at=1_699_999_950.0)
        # Adaptive 0% vs legacy 100%: clear, explainable collapse.
        self._seed_canary_bucket(adaptive_success=0.0, legacy_success=1.0)
        verdict = rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertTrue(verdict["triggered"])
        res = rollout_policy.maybe_auto_rollback(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", reason="guard unit test")
        self.assertEqual(res["new_percentage"], 0)
        self.assertEqual(res["action"], "auto_rollback")
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(history[0]["action"], "auto_rollback")

    def test_guard_quiet_on_insufficient_samples(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        verdict = rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertFalse(verdict["triggered"])
        self.assertIn("insufficient", verdict["reason"].lower())

    def test_guard_disabled_never_triggers(self):
        self._seed_canary_bucket(adaptive_success=0.0, legacy_success=1.0)
        with patch.dict("os.environ",
                        {"HERDR_ROLLOUT_GUARD_ENABLED": "0"}):
            verdict = rollout_policy.evaluate_guard(
                self.db_path, agent="codex", node="implementation",
                task_type="fix")
        self.assertFalse(verdict["triggered"])

    def test_guard_read_is_not_starved_by_a_busy_sibling_bucket(self):
        # Reviewer P1: the guard must scope its matched-row budget to
        # the target bucket. With only node/task_type filtering, a busy
        # sibling recommendation consumes the whole budget and the
        # target reads as "no samples" -> guard stays quiet while the
        # target is actually collapsing.
        _seed_collapsed_bucket(self.store, self.db_path, test=self)  # codex
        # A much busier sibling bucket: same node/task_type, a different
        # recommendation, and 240 decisions — more than the guard's
        # 200-decision budget. Scanned newest-first without an exact
        # bucket filter, all 200 matched slots go to claude and codex is
        # never reached, so the guard would read "no samples" and stay
        # quiet while codex is collapsing.
        self._seed_canary_bucket(
            adaptive_success=1.0, legacy_success=1.0,
            adaptive_n=120, legacy_n=120, recommended="claude")
        verdict = rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertIsNotNone(verdict["bucket"],
                             "the target bucket must be scanned, not starved")
        self.assertEqual(verdict["bucket"]["recommended_agent"], "codex")
        self.assertTrue(verdict["triggered"],
                        "a starved read would wrongly report quiet")

    def _record_noise_decision(self, index, *, recommended, node, task_type,
                               mode):
        """One canary/shadow route_decision with no settled outcome.

        Only the decision stream matters here: the point is how many
        unrelated rows sit between the scan start and the target bucket.
        """
        from herdr import adaptive_router
        ts = 1_700_500_000.0 + index
        run_id, task_id = f"noise-run-{index}", f"noise-task-{index}"
        if mode == "canary":
            payload = adaptive_router.build_canary_decision(
                workflow_id="wf-noise", run_id=run_id, task_id=task_id,
                node=node, task_type=task_type, actual_agent=recommended,
                recommended_agent=recommended, legacy_agent="opencode",
                diverted=False, rankings=[], gate={}, created_at=ts)
        else:
            payload = adaptive_router.build_shadow_decision(
                workflow_id="wf-noise", run_id=run_id, task_id=task_id,
                node=node, task_type=task_type, actual_agent=recommended,
                rankings=[], created_at=ts)
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-noise", node_id=node,
            task_id=task_id, agent_id=recommended,
            source=f"adaptive-router-{mode}", timestamp=ts, run_id=run_id)

    def test_guard_scan_budget_is_not_consumed_by_other_buckets(self):
        # Reviewer P1: the guard's read must scope in SQL, not in Python.
        # `scanned` counts every row a page returns, so filtering after
        # the read still lets unrelated decisions eat scan_cap and the
        # target bucket is never seen -> guard silent on a failing bucket.
        _seed_collapsed_bucket(self.store, self.db_path, test=self)  # codex
        # 200+ decisions of unrelated kinds, all NEWER than the target's.
        # scan_cap is 400, so 400 raw rows would exhaust it before the
        # target is reached if scoping happened after the read.
        for index in range(300):
            self._record_noise_decision(
                index, recommended="claude" if index % 2 else "opencode",
                node="implementation", task_type="fix", mode="canary")
        for index in range(100):
            self._record_noise_decision(
                1000 + index, recommended="codex", node="implementation",
                task_type="fix", mode="shadow")
        verdict = rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertIsNotNone(verdict["bucket"],
                             "scan budget was consumed by unrelated rows")
        self.assertEqual(verdict["bucket"]["recommended_agent"], "codex")
        self.assertTrue(verdict["triggered"])

    def test_exact_bucket_read_is_scoped_in_sql(self):
        # The pushdown must be a source-level predicate, not a Python
        # post-filter: a stricter guard budget would then be enough.
        from herdr import canary_evaluation
        for index in range(40):
            self._record_noise_decision(
                index, recommended="codex", node="implementation",
                task_type="fix", mode="canary")
        rows, meta = canary_evaluation.collect_canary_rows(
            self.db_path, node="implementation", task_type="fix",
            recommended_agent="codex", limit=5, scan_cap=5)
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(row["recommended_agent"], "codex")
            self.assertEqual(row["node"], "implementation")
        # The budget is spent only on rows that can match.
        self.assertLessEqual(meta["source_window_size"], 5)

    def test_shadow_evaluation_meta_counters_are_unaffected(self):
        # The pushdown is opt-in via recommended_agent precisely so the
        # shadow collection can still count what its filter removed.
        from herdr import shadow_evaluation
        self._record_noise_decision(
            1, recommended="codex", node="n", task_type="t", mode="canary")
        self._record_noise_decision(
            2, recommended="claude", node="n", task_type="t", mode="shadow")
        report = shadow_evaluation.run_shadow_evaluation(self.db_path)["report"]
        self.assertEqual(report["collection"]["skipped_canary_events"], 1)

    def test_guard_is_scoped_to_its_own_bucket(self):
        # A collapsed codex/implementation/fix bucket must not condemn a
        # different node, task_type, or recommended agent.
        self._seed_canary_bucket(adaptive_success=0.0, legacy_success=1.0)
        for node, task_type, agent in (
            ("test", "fix", "codex"),
            ("implementation", "feat", "codex"),
            ("implementation", "fix", "claude"),
        ):
            verdict = rollout_policy.evaluate_guard(
                self.db_path, agent=agent, node=node, task_type=task_type)
            self.assertFalse(
                verdict["triggered"],
                f"{agent}/{node}/{task_type} must stay independent")

    def test_guard_read_is_bounded(self):
        # The guard must never run an unbounded decision scan.
        from herdr import canary_evaluation as _ce
        with patch.object(
            _ce, "run_canary_evaluation",
            wraps=_ce.run_canary_evaluation) as spy:
            self._seed_canary_bucket(adaptive_success=0.0,
                                     legacy_success=1.0)
            rollout_policy.evaluate_guard(
                self.db_path, agent="codex", node="implementation",
                task_type="fix")
        filters = spy.call_args.kwargs["filters"]
        self.assertEqual(filters.limit, rollout_policy.GUARD_DECISION_LIMIT)
        self.assertEqual(filters.scan_cap, rollout_policy.GUARD_SCAN_CAP)

    def test_hot_guard_is_opt_in(self):
        # Default off: the dispatch hot path must not pay for the
        # evaluation scan. Opted in, a collapsed bucket forces Legacy.
        self._seed_canary_bucket(adaptive_success=0.0, legacy_success=1.0)
        self.assertFalse(rollout_policy.hot_guard_enabled())
        self.assertFalse(rollout_policy.should_force_legacy(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"))
        with patch.dict("os.environ", {"HERDR_ROLLOUT_HOT_GUARD": "1"}):
            self.assertTrue(rollout_policy.should_force_legacy(
                self.db_path, agent="codex", node="implementation",
                task_type="fix"))

    def test_noop_change_writes_no_audit(self):
        first = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        self.assertEqual(first["action"], "promote")
        repeat = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="again", source="cli")
        self.assertEqual(repeat["action"], "noop")
        self.assertFalse(repeat["changed"])
        self.assertEqual(len(rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")), 1)

    def test_off_on_already_off_is_noop(self):
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="already off",
            source="cli")
        self.assertEqual(record["action"], "noop")
        self.assertEqual(rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"), [])

    def test_concurrent_operators_never_lose_an_update(self):
        # Two operators racing the same bucket: exactly one wins, the
        # other gets a conflict, and no state change lacks an audit.
        import threading
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        barrier = threading.Barrier(2)
        results = []

        def promote(target):
            barrier.wait()
            try:
                rollout_policy.set_stage(
                    self.db_path, agent="codex", node="implementation",
                    task_type="fix", new_percentage=target,
                    reason=f"racer to {target}", source=f"cli:{target}")
                results.append(("ok", target))
            except ValueError as exc:
                results.append(("conflict", str(exc)))

        threads = [threading.Thread(target=promote, args=(10,)),
                   threading.Thread(target=promote, args=(25,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sum(1 for status, _ in results if status == "ok"), 1)
        self.assertEqual(
            sum(1 for status, _ in results if status == "conflict"), 1)
        final = rollout_policy.get_stage(
            self.db_path, "codex", "implementation", "fix")
        self.assertIn(final, (10, 25))
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 2)
        # The surviving history must chain without a gap.
        newest = history[0]
        self.assertEqual(newest["new_percentage"], final)
        self.assertEqual(history[1]["new_percentage"], 5)


class TestRolloutCLI(unittest.TestCase):
    """Real CLI entry: bin/herdr-task rollout ... (imperative shell)."""

    @staticmethod
    def _load_cli():
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_loader(
            "herdr-task-rollout",
            importlib.machinery.SourceFileLoader(
                "herdr-task-rollout", str(root / "bin" / "herdr-task")),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def setUp(self):
        self.store, self.db_path = _make_store(self)
        self.cli = self._load_cli()
        self.env = patch.dict(
            "os.environ",
            {"HERDR_STATE_DB": str(self.db_path),
             "HERDR_ADAPTIVE_ROLLOUT_ENABLED": "true",
             "HERDR_ROLLOUT_GUARD_ENABLED": "0"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = 0
            try:
                self.cli.cmd_rollout(self._args(argv))
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _args(argv):
        """Build the argparse-shaped namespace the CLI commands read."""
        args = type("Args", (), {})()
        args.rollout_command = argv[0]
        args.json = False
        flags = {"json"}
        index = 1
        while index < len(argv):
            token = argv[index]
            if token.lstrip("-") in flags:
                setattr(args, token.lstrip("-").replace("-", "_"), True)
                index += 1
                continue
            setattr(args, token.lstrip("-").replace("-", "_"),
                    argv[index + 1])
            index += 2
        return args

    def test_status_set_off_history_chain(self):
        code, out, _ = self._run(["status"])
        self.assertEqual(code, 0)
        self.assertIn("no staged buckets", out)

        for pct in ("5", "10", "25", "50"):
            code, out, _ = self._run([
                "set", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix", "--percentage", pct, "--reason", "r"])
            self.assertEqual(code, 0, out)
            self.assertIn("action=promote", out)

        code, out, _ = self._run(["status"])
        self.assertIn("current: 50%", out)

        code, out, _ = self._run([
            "off", "--agent", "codex", "--node", "implementation",
            "--task-type", "fix", "--reason", "emergency"])
        self.assertEqual(code, 0, out)
        self.assertIn("-> off", out)

        code, out, _ = self._run(["history"])
        self.assertEqual(code, 0)
        self.assertIn("emergency", out)
        # Full ladder plus the emergency rollback, one row each.
        self.assertEqual(len(rollout_policy.get_history(self.db_path)), 5)

    def test_set_adopts_config_fallback_then_promotes(self):
        # Live CLI path: the canary config serves 5% for the bucket, so
        # `rollout set 5` must take ownership (action=takeover, not a
        # no-op), after which `set 10` promotes 5 -> 10 instead of being
        # rejected as 0 -> 10.
        cfg = self.db_path.parent / "route-canary.json"
        cfg.write_text(json.dumps({
            "enabled": True, "percentage": 5,
            "buckets": [{"agent": "codex", "node": "implementation",
                         "task_type": "fix"}],
        }), encoding="utf-8")
        with patch.dict("os.environ",
                        {"HERDR_ROUTE_CANARY_CONFIG": str(cfg)}):
            code, out, _ = self._run([
                "set", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix", "--percentage", "5",
                "--reason", "adopt the config slice"])
            self.assertEqual(code, 0, out)
            self.assertIn("5% -> 5%", out)
            self.assertIn("action=takeover", out)

            code, out, _ = self._run(["status"])
            self.assertEqual(code, 0, out)
            self.assertIn("current: 5%", out)

            code, out, _ = self._run([
                "set", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix", "--percentage", "10",
                "--reason", "grow after review"])
            self.assertEqual(code, 0, out)
            self.assertIn("5% -> 10%", out)
            self.assertIn("action=promote", out)

            code, out, _ = self._run(["history"])
            self.assertEqual(code, 0, out)
            self.assertIn("takeover", out)
            self.assertIn("adopt the config slice", out)

    def test_skip_stage_exits_two_and_keeps_state(self):
        self._run(["set", "--agent", "codex", "--node", "implementation",
                   "--task-type", "fix", "--percentage", "5",
                   "--reason", "init"])
        code, _, err = self._run([
            "set", "--agent", "codex", "--node", "implementation",
            "--task-type", "fix", "--percentage", "50", "--reason", "skip"])
        self.assertEqual(code, 2)
        self.assertIn("5 -> 50", err)
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 5)

    def test_open_percentage_rejected(self):
        for bad in ("75", "100", "7"):
            code, _, err = self._run([
                "set", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix", "--percentage", bad,
                "--reason", "r"])
            self.assertEqual(code, 2, bad)
            self.assertIn("off/5/10/25/50", err)

    def test_reason_required(self):
        args = self._args([
            "set", "--agent", "codex", "--node", "implementation",
            "--task-type", "fix", "--percentage", "5", "--reason", "  "])
        out, err = StringIO(), StringIO()
        with (redirect_stdout(out), redirect_stderr(err),
                self.assertRaises(SystemExit) as raised):
            self.cli.cmd_rollout(args)
        self.assertEqual(int(raised.exception.code or 0), 2)
        self.assertIn("--reason is required", err.getvalue())
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 0)

    def test_check_guard_reports_facts_only(self):
        code, out, _ = self._run([
            "check-guard", "--agent", "codex", "--node", "implementation",
            "--task-type", "fix", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["triggered"])
        self.assertIn("reason", payload)

    def test_status_json_reports_kill_switch(self):
        self._run(["set", "--agent", "codex", "--node", "implementation",
                   "--task-type", "fix", "--percentage", "5",
                   "--reason", "init"])
        with patch.dict("os.environ",
                        {"HERDR_ADAPTIVE_ROLLOUT_ENABLED": "false"}):
            _, out, _ = self._run(["status", "--json"])
        payload = json.loads(out)
        self.assertTrue(payload["killed"])
        self.assertEqual(payload["states"][0]["percentage"], 5)

    def test_read_and_write_share_one_db_resolver(self):
        # Reviewer P1: rollout must not resolve its database twice. With
        # a non-default layout, a write going through _get_store() and a
        # read going through resolve_state_db_path() can land on two
        # different files: "set" succeeds, "status" shows nothing.
        with tempfile.TemporaryDirectory(prefix="herdr-rollout-split-") as tmp:
            layout = Path(tmp) / "layout"
            layout.mkdir()
            env = {"HERDR_STATE_DB": "", "TASKS_FILE": "",
                   "WORKFLOW_FILE": str(layout / "workflow.json"),
                   "WORKFLOWS_FILE": "", "CHECKPOINTS_DIR": ""}
            with patch.dict("os.environ", env):
                self._run(["set", "--agent", "codex", "--node", "n",
                           "--task-type", "t", "--percentage", "5",
                           "--reason", "r"])
                code, out, err = self._run(["status"])
            self.assertEqual(code, 0, err)
            self.assertIn("codex / n / t current: 5%", out)

    def test_check_guard_exits_nonzero_when_unavailable(self):
        # Reviewer P2: an unavailable guard must never look like a
        # passing check to a monitoring job.
        verdict = {
            "triggered": False, "status": "unavailable",
            "reason": "canary evaluation unavailable: OperationalError",
            "bucket": None, "config": {},
        }
        with patch("herdr.rollout_policy.evaluate_guard",
                   return_value=verdict):
            code, out, err = self._run([
                "check-guard", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix"])
        self.assertEqual(code, 1)
        self.assertIn("could not evaluate", err)
        self.assertIn("unavailable", err)
        self.assertEqual(out, "")

    def test_check_guard_exits_nonzero_when_unavailable_json(self):
        verdict = {
            "triggered": False, "status": "unavailable",
            "reason": "canary evaluation unavailable: OperationalError",
            "bucket": None, "config": {},
        }
        with patch("herdr.rollout_policy.evaluate_guard",
                   return_value=verdict):
            code, out, err = self._run([
                "check-guard", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix", "--json"])
        self.assertEqual(code, 1)
        # The machine-readable verdict goes to stderr so stdout stays
        # clean for anything parsing it.
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["status"], "unavailable")

    def test_check_guard_still_exits_zero_when_merely_insufficient(self):
        verdict = {
            "triggered": False, "status": "insufficient_samples",
            "reason": "insufficient samples: settled=0 < min=20",
            "bucket": None, "config": {},
        }
        with patch("herdr.rollout_policy.evaluate_guard",
                   return_value=verdict):
            code, out, _ = self._run([
                "check-guard", "--agent", "codex", "--node", "implementation",
                "--task-type", "fix"])
        self.assertEqual(code, 0)
        self.assertIn("insufficient_samples", out)

    def test_status_does_not_manufacture_schema(self):
        # Reviewer P2: a read must not create tables or migrate. #102
        # contract: observing state never manufactures state.
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="herdr-rollout-ro-") as tmp:
            db_path = Path(tmp) / "state.db"
            # A pre-rollout database: valid, existing, no rollout tables.
            sqlite3.connect(str(db_path)).close()
            self.assertFalse(_has_rollout_tables(db_path))
            with patch.dict("os.environ", {"HERDR_STATE_DB": str(db_path)}):
                code, out, _ = self._run(["status"])
                self.assertEqual(code, 0)
                self.assertIn("no staged buckets", out)
                code, out, _ = self._run(["history"])
                self.assertEqual(code, 0)
                self.assertIn("no audit events", out)
            self.assertFalse(
                _has_rollout_tables(db_path),
                "read-only rollout commands must not create schema")

    def test_status_never_prints_all_off_on_read_failure(self):
        # Reviewer P2: an unreadable state is "unknown", not "all off".
        self._run(["set", "--agent", "codex", "--node", "implementation",
                   "--task-type", "fix", "--percentage", "5",
                   "--reason", "init"])
        with patch("herdr.state_db.list_rollout_states",
                   side_effect=sqlite3_operational_error()):
            code, out, err = self._run(["status"])
        self.assertEqual(code, 1)
        self.assertIn("unavailable", err.lower())
        self.assertNotIn("all off", out + err)

    def test_history_never_prints_empty_on_read_failure(self):
        with patch("herdr.state_db.list_rollout_audit",
                   side_effect=sqlite3_operational_error()):
            code, out, err = self._run(["history"])
        self.assertEqual(code, 1)
        self.assertIn("unavailable", err.lower())
        self.assertNotIn("no audit events", out)


def _has_rollout_tables(db_path):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    return "rollout_state" in names and "rollout_audit" in names


def sqlite3_operational_error():
    import sqlite3
    return sqlite3.OperationalError("database is locked")


class TestRolloutRouterIntegration(unittest.TestCase):
    """Rollout overrides canary config percentage without touching hash."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-rollout-r-")
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        env_patch = patch.dict(
            "os.environ",
            {"HERDR_OUTCOME_AUTOFINALIZE": "0",
             "HERDR_ADAPTIVE_ROLLOUT_ENABLED": "true",
             "HERDR_ROLLOUT_GUARD_ENABLED": "0"},
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.store = get_state_store(self.tmp_path / "state.db")
        self.db_path = self.tmp_path / "state.db"
        self.patchers = [
            patch("herdr.agent_router._get_store", return_value=self.store),
            patch("herdr.agent_router.POOLS_FILE",
                  self.tmp_path / "agent-pools.json"),
            patch("herdr.agent_router.RESERVATIONS_FILE",
                  self.tmp_path / "agent-reservations.json"),
            patch("herdr.agent_router.ROUTER_LOCK_FILE",
                  self.tmp_path / "agent-router.lock"),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.store.save_workflow({
            "workflow_id": "wf-rollout", "project_id": "test-proj",
            "status": "running", "healthy_agents": [],
            "unhealthy_agents": {},
        })
        # Seed sufficient shadow history so admission passes.
        from herdr import adaptive_router as _ar
        from herdr import eval_store as _es
        from herdr import execution_outcome as _eo
        base = 1_700_000_000.0
        for i in range(30):
            run_id, task_id = f"hist-run-codex-{i}", f"hist-task-codex-{i}"
            self.store.save_task({
                "task_id": task_id, "workflow_id": "wf-rollout",
                "run_id": run_id, "node": "implementation",
                "stage": "implementation", "task_type": "fix",
                "agent": "codex", "status": "completed",
                "status_history": [{"to": s} for s in (
                    "pending", "dispatched", "working", "agent_done",
                    "completed")],
                "started_at": base + i, "finished_at": base + i + 600.0,
                "created_at": base + i,
            })
            _es.record_eval_result(
                run_id, requirements_satisfied=True,
                verification_passed=True, human_intervention_count=0,
                final_status="completed", task_id=task_id,
                workflow_id="wf-rollout", created_at=base + i + 601.0,
                db_path=self.db_path)
            out = _eo.finalize_execution_outcome(
                task_id, db_path=self.db_path,
                finalized_at=base + i + 605.0)
            assert out["status"] == "created", out
            rankings = _ar.rank_candidates(
                ["codex"], db_path=self.db_path, node="implementation",
                task_type="fix", cutoff=base + 50_000 + i + 1.0)
            payload = _ar.build_shadow_decision(
                workflow_id="wf-rollout", run_id=run_id, task_id=task_id,
                node="implementation", task_type="fix", actual_agent="codex",
                rankings=rankings, created_at=base + 50_000 + i + 2.0)
            self.store.record_event(
                "route_decision", payload, workflow_id="wf-rollout",
                node_id="implementation", task_id=task_id, agent_id="codex",
                source="adaptive-router-shadow",
                timestamp=base + 50_000 + i + 2.0, run_id=run_id)
        cfg_path = self.tmp_path / "route-canary.json"
        import json as _j
        cfg_path.write_text(_j.dumps({
            "enabled": True, "percentage": 100,
            "buckets": [{"agent": "codex", "node": "implementation",
                         "task_type": "fix"}],
        }), encoding="utf-8")
        cfg_patch = patch.dict(
            "os.environ", {"HERDR_ROUTE_CANARY_CONFIG": str(cfg_path)})
        cfg_patch.start()
        self.addCleanup(cfg_patch.stop)

    def _route(self, task_id, run_id, requested="auto"):
        from herdr import agent_router as _router
        with patch("herdr.agent_router.workflow_config_for",
                   return_value={"nodes": [{
                       "id": "implementation",
                       "agent_policy": {"preferred": ["opencode", "codex"]}}]}):
            return _router.choose_agent(
                "wf-rollout", "implementation", "fix", requested=requested,
                reservation_key=task_id, run_id=run_id)

    def _promote(self, pct):
        # Walk the ladder; tests start from off.
        ladder = [5, 10, 25, 50]
        for stage in ladder:
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=stage,
                reason=f"test promote to {stage}", source="cli:test")
            if stage == pct:
                break

    def test_no_row_preserves_canary_config(self):
        # Rollout not yet adopted: identical to #103 (config 100 diverts).
        selected = self._route("task-rr-1", "run-rr-1")
        self.assertEqual(selected, "codex")

    def test_staged_percentage_overrides_config(self):
        self._promote(5)
        # Find one identity inside 5% and one outside: rollout only
        # changes effective_percentage, hash identity is untouched.
        inside = outside = None
        for i in range(200):
            task_id, run_id = f"task-ro-{i}", f"run-ro-{i}"
            bucket = canary_router.canary_hash_bucket(task_id, run_id)
            if bucket < 5 and inside is None:
                inside = (task_id, run_id)
            if bucket >= 5 and outside is None:
                outside = (task_id, run_id)
            if inside and outside:
                break
        self.assertIsNotNone(inside)
        self.assertIsNotNone(outside)
        self.assertEqual(self._route(*inside), "codex")
        self.assertEqual(self._route(*outside), "opencode")
        payload = self.store.list_events(
            event_type="route_decision")[-1]["payload"]
        self.assertEqual(payload["canary_gate"]["effective_percentage"], 5)

    def test_kill_switch_stops_diversion_immediately(self):
        self._promote(50)
        # A hash-0 identity is diverted at every stage, so it proves the
        # kill switch stops the canary slice even at 50%.
        identity = None
        for i in range(500):
            task_id, run_id = f"task-kill-{i}", f"run-kill-{i}"
            if canary_router.canary_hash_bucket(task_id, run_id) == 0:
                identity = (task_id, run_id)
                break
        self.assertIsNotNone(identity)
        self.assertEqual(self._route(*identity), "codex")
        with patch.dict("os.environ",
                        {"HERDR_ADAPTIVE_ROLLOUT_ENABLED": "false"}):
            # Same identity, same process: the kill switch is read per
            # routing, so diversion stops immediately with no restart.
            selected = self._route(*identity)
            self.assertEqual(selected, "opencode")
            payload = self.store.list_events(
                event_type="route_decision")[-1]["payload"]
            self.assertEqual(payload["mode"], "canary")
            self.assertEqual(payload["canary_gate"]["effective_percentage"], 0)
            self.assertFalse(payload["diverted"])
        states = rollout_policy.list_states(self.db_path)
        self.assertTrue(any(s["percentage"] == 50 for s in states))
        # History survives the kill switch for later audit.
        self.assertEqual(len(rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")), 4)

    def test_rollout_read_failure_never_expands_traffic(self):
        # A rollout read failure must resolve to 0, not back to the
        # pre-rollout canary config percentage.
        self._promote(50)
        with patch("herdr.rollout_policy.effective_percentage",
                   side_effect=RuntimeError("rollout db down")):
            selected = self._route("task-dbf-1", "run-dbf-1")
        self.assertEqual(selected, "opencode")

    def test_explicit_request_still_bypasses(self):
        self._promote(50)
        selected = self._route("task-exp-1", "run-exp-1", requested="claude")
        self.assertEqual(selected, "claude")

    def test_diversion_still_requires_persisted_decision(self):
        import sqlite3 as _sqlite3
        self._promote(50)
        real_record = self.store.record_event

        def failing_record(event_type, payload, **kwargs):
            if event_type == "route_decision" and isinstance(payload, dict) \
                    and payload.get("mode") == "canary":
                raise _sqlite3.OperationalError("database is locked")
            return real_record(event_type, payload, **kwargs)

        with patch.object(self.store, "record_event",
                          side_effect=failing_record):
            selected = self._route("task-pf-1", "run-pf-1")
        self.assertEqual(selected, "opencode")

    def test_stale_writer_cannot_overwrite_new_state(self):
        from herdr import state_db as _sdb
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="fresh",
            source="cli:test")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="fresh",
            source="cli:test")
        # A stale operator holding the 5% snapshot must not overwrite 10.
        with self.assertRaises(ValueError):
            _sdb.transact_rollout_stage(
                agent="codex", node="implementation", task_type="fix",
                expected=_sdb.RolloutSnapshot(exists=True, percentage=5),
                write=_sdb.RolloutWrite(
                    new_percentage=25, previous_percentage=5,
                    action="promote", reason="stale", source="cli:stale",
                    algorithm_version=rollout_policy.ALGORITHM_VERSION,
                    created_at=1_700_000_000.0),
                db_path=self.db_path)
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 10)
        # No audit row for the rejected stale write.
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 2)

    def test_stale_promotion_cannot_overwrite_emergency_rollback(self):
        # Reviewer P1: presence must be part of the CAS identity.
        # T1 (promotion) reads "absent"; T2 (emergency off) writes an
        # explicit 0 row; T1's percentage still looks like 0, so a
        # percentage-only CAS would happily overwrite the rollback.
        from herdr import state_db as _sdb
        stale = _sdb.read_rollout_snapshot(
            "codex", "implementation", "fix", self.db_path)
        self.assertFalse(stale.exists)
        # T2: the emergency rollback lands first.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="emergency",
            source="cli:rollback", config_fallback=50)
        # T1: decided against the "absent" snapshot it observed.
        decision = rollout_policy.decide_rollout_change(
            stale, new_percentage=5, config_fallback=50)
        self.assertFalse(decision.is_noop)
        with self.assertRaises(ValueError) as raised:
            _sdb.transact_rollout_stage(
                agent="codex", node="implementation", task_type="fix",
                expected=stale,
                write=_sdb.RolloutWrite(
                    new_percentage=5,
                    previous_percentage=decision.effective_prev,
                    action=decision.action, reason="stale promotion",
                    source="cli:stale",
                    algorithm_version=rollout_policy.ALGORITHM_VERSION,
                    created_at=1_700_000_000.0),
                db_path=self.db_path)
        self.assertIn("conflict", str(raised.exception))
        # The rollback survives untouched.
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 0)
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=50), 0)
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "rollback")

    def test_noop_rollback_is_also_compare_and_swap(self):
        # Reviewer P1: a no-op must be re-verified inside the
        # transaction, never returned from a stale pre-read.
        from herdr import state_db as _sdb
        stale = _sdb.read_rollout_snapshot(
            "codex", "implementation", "fix", self.db_path)
        # Someone promotes 0 -> 5 after our read.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="racer",
            source="cli:racer")
        decision = rollout_policy.decide_rollout_change(
            stale, new_percentage=0)
        self.assertTrue(decision.is_noop,
                        "against the stale snapshot this is a no-op")
        with self.assertRaises(ValueError):
            _sdb.transact_rollout_stage(
                agent="codex", node="implementation", task_type="fix",
                expected=stale, write=None, db_path=self.db_path)
        # The live 5% is never reported as "already off".
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 5)
        self.assertEqual(len(rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")), 1)

    def test_corrupt_stage_can_still_be_rolled_back_to_off(self):
        # Reviewer P2: a stage outside the closed enum must not strand
        # the operator. Rewriting the snapshot for the CAS would make
        # every attempt conflict against the corrupt row on disk.
        from herdr import state_db as _sdb
        corrupt = _sdb.RolloutSnapshot(exists=True, percentage=75)
        _sdb.transact_rollout_stage(
            agent="codex", node="implementation", task_type="fix",
            expected=_sdb.RolloutSnapshot(exists=False, percentage=0),
            write=_sdb.RolloutWrite(
                new_percentage=75, previous_percentage=0, action="rollback",
                reason="simulated corruption", source="test",
                algorithm_version=rollout_policy.ALGORITHM_VERSION,
                created_at=1_700_000_000.0),
            db_path=self.db_path)
        live = _sdb.read_rollout_snapshot(
            "codex", "implementation", "fix", self.db_path)
        self.assertEqual(live, corrupt)
        # Routing already treats a corrupt stage as serving nothing.
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=50), 0)
        # While corrupt, only off is allowed: corruption is not a licence
        # to promote to an arbitrary stage.
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=5, reason="promote",
                source="cli", config_fallback=50)
        # "off" must repair it, not conflict, and not report a no-op.
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="repair",
            source="cli", config_fallback=50)
        self.assertEqual(record["action"], "rollback")
        self.assertTrue(record["changed"])
        self.assertEqual(_sdb.read_rollout_snapshot(
            "codex", "implementation", "fix", self.db_path),
            _sdb.RolloutSnapshot(exists=True, percentage=0))
        # Repaired, the bucket is an ordinary off row again.
        self.assertEqual(rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="promote now",
            source="cli", config_fallback=50)["action"], "promote")

    def test_corrupt_stage_never_promotes_automatically(self):
        from herdr import state_db as _sdb
        _sdb.transact_rollout_stage(
            agent="codex", node="implementation", task_type="fix",
            expected=_sdb.RolloutSnapshot(exists=False, percentage=0),
            write=_sdb.RolloutWrite(
                new_percentage=75, previous_percentage=0, action="rollback",
                reason="simulated corruption", source="test",
                algorithm_version=rollout_policy.ALGORITHM_VERSION,
                created_at=1_700_000_000.0),
            db_path=self.db_path)
        # The guard may only ever write off.
        with self.assertRaises(ValueError):
            rollout_policy.decide_rollout_change(
                _sdb.RolloutSnapshot(exists=True, percentage=75),
                new_percentage=5)

    def test_bucket_key_is_unambiguous(self):
        # Reviewer P2: a "/"-joined key cannot tell ("a/b","c","d") from
        # ("a","b/c","d"), so two buckets could share one row.
        from herdr import state_db as _sdb
        first = _sdb.rollout_bucket_key("a/b", "c", "d")
        second = _sdb.rollout_bucket_key("a", "b/c", "d")
        self.assertNotEqual(first, second)
        # And storage is keyed by the real columns, not by that string.
        rollout_policy.set_stage(
            self.db_path, agent="a/b", node="c", task_type="d",
            new_percentage=5, reason="r", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="a", node="b/c", task_type="d",
            new_percentage=5, reason="r", source="cli")
        # Two rows, two keys: a shared string key would have merged them.
        states = {s["bucket_key"] for s in rollout_policy.list_states(
            self.db_path)}
        self.assertEqual(len(states), 2)
        # Moving one leaves the other alone.
        rollout_policy.set_stage(
            self.db_path, agent="a/b", node="c", task_type="d",
            new_percentage=0, reason="rollback", source="cli")
        self.assertEqual(rollout_policy.get_stage(
            self.db_path, "a/b", "c", "d"), 0)
        states = {s["bucket_key"] for s in rollout_policy.list_states(
            self.db_path)}
        self.assertEqual(len(states), 2)

    def test_single_snapshot_read_covers_both_fields(self):
        # One query, one point in time: the caller can never stitch
        # exists/percentage from two different reads.
        from herdr import state_db as _sdb
        snapshot = _sdb.read_rollout_snapshot(
            "codex", "implementation", "fix", self.db_path)
        self.assertEqual(
            snapshot, _sdb.RolloutSnapshot(exists=False, percentage=0))
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        self.assertEqual(
            _sdb.read_rollout_snapshot(
                "codex", "implementation", "fix", self.db_path),
            _sdb.RolloutSnapshot(exists=True, percentage=5))


class TestFallbackTakeover(unittest.TestCase):
    """absent row + config fallback N + set N is a takeover, not a no-op.

    A takeover moves bucket ownership from the canary config to an
    explicit staged row without moving traffic. It is a real state
    mutation with its own audit action, and it is the only reason the
    promotion ladder at fallback-served buckets is reachable at all.
    """

    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def test_takeover_persists_row_and_unblocks_ladder(self):
        # Config owns 5% for the bucket; no staged row exists yet.
        self.assertFalse(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="adopt config 5%",
            source="cli", config_fallback=5)
        self.assertTrue(record["changed"])
        self.assertEqual(record["action"], "takeover")
        # Traffic did not move; ownership did.
        self.assertEqual(record["previous_percentage"], 5)
        self.assertEqual(record["new_percentage"], 5)
        self.assertEqual(
            state_db.read_rollout_snapshot(
                "codex", "implementation", "fix", self.db_path),
            state_db.RolloutSnapshot(exists=True, percentage=5))
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=5), 5)

        # The ladder now validates against the real staged position:
        # 5 -> 10 is legal, where before the takeover it read 0 -> 10.
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="grow",
            source="cli", config_fallback=5)
        self.assertEqual(record["action"], "promote")
        self.assertEqual(record["previous_percentage"], 5)
        self.assertEqual(record["new_percentage"], 10)
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual([row["action"] for row in history],
                         ["promote", "takeover"])
        self.assertEqual(history[1]["previous_percentage"], 5)
        self.assertEqual(history[1]["new_percentage"], 5)

    def test_explicit_stage_at_same_value_is_a_true_noop(self):
        # exists=5 + set 5: ownership already moved, nothing to do.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="take over",
            source="cli", config_fallback=5)
        record = rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="again",
            source="cli", config_fallback=5)
        self.assertEqual(record["action"], "noop")
        self.assertFalse(record["changed"])
        self.assertEqual(
            len(rollout_policy.get_history(
                self.db_path, agent="codex", node="implementation",
                task_type="fix")), 1)

    def test_promotion_without_takeover_still_rejected(self):
        # 0 -> 10 stays invalid while the bucket row is absent: the
        # takeover at the served percentage is the explicit entry step.
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=10, reason="skip",
                source="cli", config_fallback=5)
        self.assertFalse(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))

    def test_takeover_at_higher_config_level_changes_ownership_only(self):
        decision = rollout_policy.decide_rollout_change(
            state_db.RolloutSnapshot(exists=False, percentage=0),
            new_percentage=25, config_fallback=25)
        self.assertFalse(decision.is_noop)
        self.assertEqual(decision.action, "takeover")
        self.assertEqual(decision.effective_prev, 25)
        self.assertEqual(decision.new_percentage, 25)

    def test_zero_fallback_zero_target_stays_noop(self):
        # Nothing serving + nothing requested: no ownership to adopt.
        for fallback in (None, 0):
            decision = rollout_policy.decide_rollout_change(
                state_db.RolloutSnapshot(exists=False, percentage=0),
                new_percentage=0, config_fallback=fallback)
            self.assertTrue(decision.is_noop)
            self.assertEqual(decision.action, "noop")


class TestSingleSnapshotEffectiveRead(unittest.TestCase):
    """effective_percentage decides from one atomic snapshot read.

    The pre-hotfix code stitched get_rollout_stage() and
    rollout_stage_known() — two queries a committed emergency rollback
    could interleave with. The decision must come from one snapshot.
    """

    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def _forbid_two_phase_reads(self):
        def fail(name):
            def _raise(*_a, **_k):
                raise AssertionError(
                    f"two-phase read: state_db.{name} must not be called")
            return _raise
        return (
            patch.object(state_db, "get_rollout_stage",
                         side_effect=fail("get_rollout_stage")),
            patch.object(state_db, "rollout_stage_known",
                         side_effect=fail("rollout_stage_known")),
            patch.object(state_db, "read_rollout_snapshot",
                         wraps=state_db.read_rollout_snapshot),
        )

    def test_staged_row_resolved_from_exactly_one_snapshot(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        p_stage, p_known, p_snap = self._forbid_two_phase_reads()
        with p_stage, p_known, p_snap as spy:
            self.assertEqual(rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=50), 5)
        self.assertEqual(spy.call_count, 1)

    def test_absent_row_resolved_from_exactly_one_snapshot(self):
        p_stage, p_known, p_snap = self._forbid_two_phase_reads()
        with p_stage, p_known, p_snap as spy:
            self.assertEqual(rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=5), 5)
            self.assertEqual(rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=None), 0)
        self.assertEqual(spy.call_count, 2)  # one read per decision

    def test_committed_rollback_is_immediately_visible(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=None), 5)
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="emergency",
            source="guard", automatic=True)
        # The next read after the rollback COMMITs returns 0 — and with
        # a single atomic read it structurally cannot return a stale 5.
        p_stage, p_known, p_snap = self._forbid_two_phase_reads()
        with p_stage, p_known, p_snap as spy:
            self.assertEqual(rollout_policy.effective_percentage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", config_fallback=50), 0)
        self.assertEqual(spy.call_count, 1)


class TestGuardEpisodeIsolation(unittest.TestCase):
    """Guard evidence belongs to the current rollout episode only.

    Every successful explicit stage change opens a new evidence episode:
    the newest rollout_audit row's created_at is the guard's `since`
    boundary. A rollback + retry re-accumulates evidence instead of
    re-condemning the retry with the previous episode's bad samples.
    Historical rows are never deleted; they just stop being evidence.
    """

    T_ACTIVATE = 1_700_000_000.0
    T_EP1_SAMPLES = 1_700_001_000.0
    T_ROLLBACK = 1_700_002_000.0
    T_RETRY = 1_700_003_000.0
    T_EP2_GOOD = 1_700_004_000.0
    T_EP2_BAD = 1_700_005_000.0

    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def _guard(self):
        return rollout_policy.evaluate_guard(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")

    def test_retry_after_rollback_reaccumulates_evidence(self):
        # Episode 1: activate 5%, then the adaptive arm collapses.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="activate ep1",
            source="cli", created_at=self.T_ACTIVATE)
        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP1_SAMPLES,
            tag="ep1", adaptive_successes=0, adaptive_failures=12,
            legacy_successes=12, legacy_failures=0)
        verdict = self._guard()
        self.assertTrue(verdict["triggered"])

        # The guard's own write path ends episode 1.
        with patch.object(rollout_policy.time, "time",
                          return_value=self.T_ROLLBACK):
            result = rollout_policy.maybe_auto_rollback(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", reason="ep1 regression")
        self.assertEqual(result["action"], "auto_rollback")

        # Episode 2: the human re-activates 5% after fixing the issue.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="retry after fix",
            source="cli", created_at=self.T_RETRY)
        self.assertEqual(rollout_policy.current_rollout_episode_start(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"), self.T_RETRY)

        # No new samples yet: the old episode's collapse must NOT
        # re-condemn the retry. Starvation is a decision, not a verdict.
        verdict = self._guard()
        self.assertEqual(verdict["status"], "insufficient_samples")
        self.assertFalse(verdict["triggered"])
        self.assertEqual(verdict["evidence_since"], self.T_RETRY)

        # Episode-2 good evidence: tolerated.
        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP2_GOOD,
            tag="ep2good", adaptive_successes=12, adaptive_failures=0,
            legacy_successes=12, legacy_failures=0)
        verdict = self._guard()
        self.assertEqual(verdict["status"], "within_tolerance")
        self.assertFalse(verdict["triggered"])

        # Episode-2's own regression: still caught.
        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP2_BAD,
            tag="ep2bad", adaptive_successes=0, adaptive_failures=12,
            legacy_successes=0, legacy_failures=0)
        verdict = self._guard()
        self.assertTrue(verdict["triggered"])

    def test_stage_change_opens_a_fresh_evidence_window(self):
        # 5% evidence must not prove 10% safe: the promotion itself
        # opens a new episode and the evidence window restarts.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="activate",
            source="cli", created_at=self.T_ACTIVATE)
        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP1_SAMPLES,
            tag="at5", adaptive_successes=12, adaptive_failures=0,
            legacy_successes=12, legacy_failures=0)
        self.assertEqual(self._guard()["status"], "within_tolerance")

        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="grow",
            source="cli", created_at=self.T_RETRY)
        verdict = self._guard()
        self.assertEqual(verdict["status"], "insufficient_samples")
        self.assertFalse(verdict["triggered"])
        self.assertEqual(verdict["evidence_since"], self.T_RETRY)

        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP2_GOOD,
            tag="at10", adaptive_successes=12, adaptive_failures=0,
            legacy_successes=12, legacy_failures=0)
        self.assertEqual(self._guard()["status"], "within_tolerance")

    def test_bucket_without_rollout_history_reads_unscoped(self):
        # #103 behavior is preserved for config-only buckets: no rollout
        # episode exists, so the guard read stays unscoped and the old
        # samples still count.
        _seed_episode_samples(
            self, self.store, self.db_path, start_ts=self.T_EP1_SAMPLES,
            tag="plain", adaptive_successes=0, adaptive_failures=12,
            legacy_successes=12, legacy_failures=0)
        verdict = self._guard()
        self.assertTrue(verdict["triggered"])
        self.assertIsNone(verdict["evidence_since"])

    def test_episode_start_tracks_newest_change_including_rollback(self):
        self.assertIsNone(rollout_policy.current_rollout_episode_start(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"))
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="a", source="cli",
            created_at=1_000.0)
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="b", source="cli",
            created_at=2_000.0)
        self.assertEqual(rollout_policy.current_rollout_episode_start(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"), 2_000.0)
        # A rollback is a stage change too: it opens a new boundary.
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=0, reason="off", source="cli",
            created_at=3_000.0)
        self.assertEqual(rollout_policy.current_rollout_episode_start(
            self.db_path, agent="codex", node="implementation",
            task_type="fix"), 3_000.0)

    def test_episode_start_on_pre_rollout_db_is_none(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory(prefix="herdr-rollout-bare-")
        self.addCleanup(tmp.cleanup)
        bare = Path(tmp.name) / "state.db"
        sqlite3.connect(str(bare)).close()
        self.assertIsNone(rollout_policy.current_rollout_episode_start(
            bare, agent="codex", node="implementation", task_type="fix"))


class TestExactBucketIndex(unittest.TestCase):
    """The exact-bucket guard read must be genuinely index-served.

    Sparse buckets must not scan route_decision history: the partial
    expression index is asserted through EXPLAIN QUERY PLAN, and its
    CASE/json_valid wrapper keeps malformed payloads from ever breaking
    INSERT-time index maintenance.
    """

    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def _decision(self, index, *, recommended, tag, ts):
        from herdr import adaptive_router
        payload = adaptive_router.build_canary_decision(
            workflow_id="wf-idx", run_id=f"{tag}-run-{index}",
            task_id=f"{tag}-task-{index}", node="implementation",
            task_type="fix", actual_agent=recommended,
            recommended_agent=recommended, legacy_agent="opencode",
            diverted=False, rankings=[], gate={}, created_at=ts)
        self.store.record_event(
            "route_decision", payload, workflow_id="wf-idx",
            node_id="implementation", task_id=f"{tag}-task-{index}",
            agent_id=recommended, source="adaptive-router-canary",
            timestamp=ts, run_id=f"{tag}-run-{index}")

    @staticmethod
    def _bucket(agent="codex"):
        return state_db.ExactDecisionBucket(
            mode="canary", recommended_agent=agent,
            node="implementation", task_type="fix")

    def test_exact_bucket_query_plan_uses_the_expression_index(self):
        import sqlite3
        base = 1_700_000_000.0
        # Many sibling rows, few target rows: the sparse-bucket case.
        for index in range(300):
            self._decision(index, recommended="claude", tag="sib",
                           ts=base + index)
        for index in range(5):
            self._decision(index, recommended="codex", tag="tgt",
                           ts=base + 1_000 + index)

        sql, params = state_db._route_decisions_sql(
            limit=200, since=base, exact_bucket=self._bucket())
        conn = sqlite3.connect(str(self.db_path))
        try:
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql,
                                params).fetchall()
        finally:
            conn.close()
        detail = " | ".join(str(row[-1]) for row in plan)
        self.assertIn("idx_events_route_decision_bucket", detail)
        self.assertIn("SEARCH events", detail)
        self.assertNotIn("SCAN events", detail)

        # The indexed read returns exactly the sparse target, newest
        # first, with no sibling leakage.
        rows = state_db.query_route_decisions(
            limit=200, since=base, db_path=self.db_path,
            exact_bucket=self._bucket())
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["run_id"], "tgt-run-4")
        for row in rows:
            self.assertEqual(row["payload"]["recommended_agent"], "codex")

    def test_shadow_bucket_shares_the_index_without_semantic_change(self):
        from herdr import adaptive_router
        base = 1_700_000_000.0
        # Canary and shadow rows interleaved in the same event stream:
        # each exact bucket must still see only its own mode.
        for index in range(10):
            self._decision(index, recommended="codex", tag="mix",
                           ts=base + index)
        for index in range(20):
            payload = adaptive_router.build_shadow_decision(
                workflow_id="wf-idx", run_id=f"sh-run-{index}",
                task_id=f"sh-task-{index}", node="implementation",
                task_type="fix", actual_agent="codex",
                rankings=[{"agent": "codex"}], created_at=base + 100 + index)
            self.store.record_event(
                "route_decision", payload, workflow_id="wf-idx",
                node_id="implementation", task_id=f"sh-task-{index}",
                agent_id="codex", source="adaptive-router-shadow",
                timestamp=base + 100 + index, run_id=f"sh-run-{index}")

        shadow_rows = state_db.query_route_decisions(
            limit=50, db_path=self.db_path,
            exact_bucket=state_db.ExactDecisionBucket(
                mode="shadow", recommended_agent="codex",
                node="implementation", task_type="fix"))
        self.assertEqual(len(shadow_rows), 20)
        for row in shadow_rows:
            self.assertEqual(row["payload"]["mode"], "shadow")

        canary_rows = state_db.query_route_decisions(
            limit=50, db_path=self.db_path, exact_bucket=self._bucket())
        self.assertEqual(len(canary_rows), 10)
        for row in canary_rows:
            self.assertEqual(row["payload"]["mode"], "canary")

    def test_malformed_payloads_never_match_and_never_break_writes(self):
        # A malformed payload indexes as NULL: the INSERT succeeds, the
        # exact bucket never matches it, and the general scan still
        # tolerates it (decoded to {}).
        conn = state_db.get_db_connection(self.db_path)
        try:
            conn.execute(
                "INSERT INTO events(node_id, event_type, payload_json, "
                "timestamp) VALUES ('implementation', 'route_decision', "
                "'{not valid json', 1_700_000_000.0)")
        finally:
            conn.close()
        rows = state_db.query_route_decisions(
            limit=10, db_path=self.db_path, exact_bucket=self._bucket())
        self.assertEqual(rows, [])
        rows = state_db.query_route_decisions(
            limit=10, db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"], {})


class TestStrictPercentageParsing(unittest.TestCase):
    """Validate before convert: no lossy int() on the rollout boundary.

    int(5.9) used to silently truncate to the valid stage 5. Legal
    inputs are exact: 0/5/10/25/50, their strings, "off", integral
    floats and Decimals. Fractional, non-finite and bool inputs raise
    ValueError at normalize_percentage and decay to 0 in _safe_fallback.
    """

    def setUp(self):
        self.store, self.db_path = _make_store(self)

    def test_integral_floats_and_decimals_accepted(self):
        from decimal import Decimal
        self.assertEqual(rollout_policy.normalize_percentage(0.0), 0)
        self.assertEqual(rollout_policy.normalize_percentage(5.0), 5)
        self.assertEqual(rollout_policy.normalize_percentage(50.0), 50)
        self.assertEqual(
            rollout_policy.normalize_percentage(Decimal("5.0")), 5)
        self.assertEqual(
            rollout_policy.normalize_percentage(Decimal("10")), 10)

    def test_fractional_rejected_never_truncated(self):
        for bad in (5.9, 5.1, 4.9999, 10.5, 25.0001, -0.5):
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                rollout_policy.normalize_percentage(bad)

    def test_non_finite_and_bool_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf"),
                    True, False):
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                rollout_policy.normalize_percentage(bad)

    def test_fractional_decimal_and_nan_decimal_rejected(self):
        from decimal import Decimal
        for bad in (Decimal("5.9"), Decimal("10.5"), Decimal("NaN"),
                    Decimal("Infinity")):
            with self.assertRaises(ValueError, msg=f"{bad!r}"):
                rollout_policy.normalize_percentage(bad)

    def test_set_stage_rejects_fractional_via_python_api(self):
        # The domain boundary rejects before any state is written.
        with self.assertRaises(ValueError):
            rollout_policy.set_stage(
                self.db_path, agent="codex", node="implementation",
                task_type="fix", new_percentage=5.9, reason="r",
                source="api")
        self.assertFalse(state_db.rollout_stage_known(
            "codex", "implementation", "fix", self.db_path))

    def test_fractional_fallback_serves_zero_never_truncates(self):
        # 5.9 as a config fallback is unusable: it resolves to 0, never
        # to a serving 5.
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=5.9), 0)
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=5.0), 5)
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=float("nan")), 0)
        self.assertEqual(rollout_policy.effective_percentage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", config_fallback=True), 0)

    def test_fractional_fallback_is_not_a_takeover(self):
        # An unusable fallback reads as 0 traffic: set 5 on it is a
        # normal 0 -> 5 promotion, never a takeover at "5".
        decision = rollout_policy.decide_rollout_change(
            state_db.RolloutSnapshot(exists=False, percentage=0),
            new_percentage=5, config_fallback=5.9)
        self.assertEqual(decision.action, "promote")
        self.assertEqual(decision.effective_prev, 0)

    def test_safe_fallback_lossless(self):
        self.assertEqual(rollout_policy._safe_fallback(None), 0)
        self.assertEqual(rollout_policy._safe_fallback(0), 0)
        self.assertEqual(rollout_policy._safe_fallback(5), 5)
        self.assertEqual(rollout_policy._safe_fallback(5.0), 5)
        self.assertEqual(rollout_policy._safe_fallback("5"), 5)
        self.assertEqual(rollout_policy._safe_fallback(5.9), 0)
        self.assertEqual(rollout_policy._safe_fallback("5.9"), 0)
        self.assertEqual(rollout_policy._safe_fallback(True), 0)
        self.assertEqual(rollout_policy._safe_fallback(float("inf")), 0)
        self.assertEqual(rollout_policy._safe_fallback(float("nan")), 0)
        self.assertEqual(rollout_policy._safe_fallback(101), 0)
        self.assertEqual(rollout_policy._safe_fallback(-1), 0)


if __name__ == "__main__":
    unittest.main()
