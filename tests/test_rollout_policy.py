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

        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="manual", source="cli")
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
                            adaptive_n=12, legacy_n=12):
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
                recommended_agent="codex", legacy_agent="opencode",
                diverted=diverted, rankings=[], gate={}, created_at=ts)
            self.store.record_event(
                "route_decision", payload, workflow_id="wf-guard",
                node_id="implementation", task_id=task_id, agent_id=actual,
                source="adaptive-router-canary", timestamp=ts, run_id=run_id)

        for i in range(adaptive_n):
            ok = i < int(adaptive_n * adaptive_success)
            run_id, task_id = f"run-a-{idx}", f"task-a-{idx}"
            ts = base + idx
            settle("codex", ok, run_id, task_id, ts)
            decide(run_id, task_id, "codex", True, ts + 200.0)
            idx += 1
        for i in range(legacy_n):
            ok = i < int(legacy_n * legacy_success)
            run_id, task_id = f"run-l-{idx}", f"task-l-{idx}"
            ts = base + idx
            settle("opencode", ok, run_id, task_id, ts)
            decide(run_id, task_id, "opencode", False, ts + 200.0)
            idx += 1

    def test_guard_triggers_on_collapsed_adaptive(self):
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=5, reason="r", source="cli")
        rollout_policy.set_stage(
            self.db_path, agent="codex", node="implementation",
            task_type="fix", new_percentage=10, reason="r", source="cli")
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
        # A stale operator holding previous=5 must not overwrite 10.
        with self.assertRaises(ValueError):
            _sdb.apply_rollout_stage_atomic(
                agent="codex", node="implementation", task_type="fix",
                previous_percentage=5, new_percentage=25,
                action="promote", reason="stale", source="cli:stale",
                algorithm_version=rollout_policy.ALGORITHM_VERSION,
                created_at=1_700_000_000.0, db_path=self.db_path)
        self.assertEqual(
            rollout_policy.get_stage(
                self.db_path, "codex", "implementation", "fix"), 10)
        # No audit row for the rejected stale write.
        history = rollout_policy.get_history(
            self.db_path, agent="codex", node="implementation",
            task_type="fix")
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()
