#!/opt/homebrew/bin/python3
"""Tests for the dispatched-delivery fuse (dispatched-but-never-working).

Regression cover for the wf-nexusarchive-0917-01 incident (2026-09-17):
the requirements-challenger task sat in `dispatched` for 2h50m (pane idle,
orchestration prompt never acked) while the sibling executor waited at the
node join. Sentinel's one-shot nudge requires the orchestration marker on
screen, so a dead pane never gets nudged; STALL alerts are notify-only and
were missed for hours.

Contract under test:
- herdr.liveness.evaluate_dispatch_fuse: pure breach detection for tasks
  stuck in `dispatched` beyond HERDR_DISPATCH_DELIVERY_SLA (default 600s),
  one breach per (task_id, updated_at) episode, requeue counts inherited
  across redispatches of the same never-started task.
- services.herdr-sentinel.check_dispatch_fuse: breach + dead-pane evidence
  -> fail the task into the existing failed -> supersede -> relaunch path
  with an actionable notification; breach + live pane (or fuse disabled) ->
  notify only, never fail a possibly-starting task.
"""

import importlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import liveness  # noqa: E402


def _task(task_id, status, updated_at, **extra):
    task = {
        "task_id": task_id,
        "status": status,
        "updated_at": updated_at,
        "workflow_id": "wf-fuse",
        "node": "requirements",
        "agent": "opencode",
        "pane_id": "wX:p9",
    }
    task.update(extra)
    return task


class DispatchFuseEvaluationTest(unittest.TestCase):
    def test_breach_raised_once_per_episode(self):
        now = 20_000.0
        tasks = [_task("t1", "dispatched", now - 3600)]

        breaches, episodes = liveness.evaluate_dispatch_fuse(tasks, {}, now, sla=600)
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["task_id"], "t1")
        self.assertEqual(breaches[0]["status"], "dispatched")
        self.assertEqual(breaches[0]["waited_seconds"], 3600)
        self.assertEqual(breaches[0]["requeues"], 0)

        again, episodes = liveness.evaluate_dispatch_fuse(tasks, episodes, now + 5, sla=600)
        self.assertEqual(again, [])

    def test_redispatch_resets_episode_but_keeps_requeues(self):
        now = 20_000.0
        tasks = [_task("t1", "dispatched", now - 3600)]
        _, episodes = liveness.evaluate_dispatch_fuse(tasks, {}, now, sla=600)
        episodes["t1"]["requeues"] = 1

        # Controller redispatched the same never-started task (new updated_at).
        tasks = [_task("t1", "dispatched", now - 700)]
        breaches, episodes = liveness.evaluate_dispatch_fuse(tasks, episodes, now, sla=600)
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0]["requeues"], 1)

    def test_non_dispatched_and_fresh_tasks_never_breach(self):
        now = 20_000.0
        tasks = [
            _task("w1", "working", now - 99999),
            _task("p1", "pending", now - 99999),
            _task("d1", "cleaned", now - 99999),
            _task("fresh", "dispatched", now - 5),
            _task("no-ts", "dispatched", 0),
        ]
        breaches, episodes = liveness.evaluate_dispatch_fuse(tasks, {}, now, sla=600)
        self.assertEqual(breaches, [])
        self.assertEqual(episodes, {})

    def test_recovery_clears_episode(self):
        now = 20_000.0
        tasks = [_task("t1", "dispatched", now - 3600)]
        _, episodes = liveness.evaluate_dispatch_fuse(tasks, {}, now, sla=600)
        self.assertIn("t1", episodes)

        tasks = [_task("t1", "working", now - 1)]
        breaches, episodes = liveness.evaluate_dispatch_fuse(tasks, episodes, now, sla=600)
        self.assertEqual(breaches, [])
        self.assertNotIn("t1", episodes)

    def test_missing_tasks_pruned_from_episodes(self):
        now = 20_000.0
        episodes = {"ghost": {"task_id": "ghost", "updated_at": now - 9999}}
        breaches, episodes = liveness.evaluate_dispatch_fuse([], episodes, now, sla=600)
        self.assertEqual(breaches, [])
        self.assertEqual(episodes, {})

    def test_sla_env_override(self):
        self.assertEqual(liveness.dispatch_delivery_sla(), 600.0)
        with patch.dict("os.environ", {"HERDR_DISPATCH_DELIVERY_SLA": "60"}):
            self.assertEqual(liveness.dispatch_delivery_sla(), 60.0)
        with patch.dict("os.environ", {"HERDR_DISPATCH_DELIVERY_SLA": "nope"}):
            self.assertEqual(liveness.dispatch_delivery_sla(), 600.0)


class InfraFailureRecoverySelectionTest(unittest.TestCase):
    """Selector contract for controller-side auto re-dispatch."""

    REASONS = {"dispatch_delivery_fuse", "agent_process_crash"}

    def _task(self, task_id, status, node="requirements", history=None, **extra):
        task = {
            "task_id": task_id,
            "status": status,
            "node": node,
            "stage": node,
            "workflow_id": "wf-fuse",
            "created_at": len(task_id),
            "status_history": history or [],
        }
        task.update(extra)
        return task

    def _failed(self, task_id, node="requirements", reason="dispatch_delivery_fuse", **extra):
        return self._task(
            task_id,
            "failed",
            node=node,
            history=[{"to": "failed", "reason": reason}],
            **extra,
        )

    def test_dead_node_with_infra_failure_is_selected(self):
        executor = self._task("wf-req-executor", "cleaned")
        challenger = self._failed("wf-req-challenger")
        selected = liveness.select_infra_failures_for_recovery(
            [executor, challenger], self.REASONS
        )
        self.assertEqual(
            [t["task_id"] for t in selected], ["wf-req-challenger"]
        )

    def test_node_with_live_task_is_skipped(self):
        live = self._task("wf-plan-executor", "working", node="plan")
        failed = self._failed("wf-plan-challenger", node="plan")
        selected = liveness.select_infra_failures_for_recovery(
            [live, failed], self.REASONS
        )
        self.assertEqual(selected, [])

    def test_quality_failure_is_not_recovered(self):
        failed = self._failed("wf-req-challenger", reason="cli_set_status")
        selected = liveness.select_infra_failures_for_recovery(
            [failed], self.REASONS
        )
        self.assertEqual(selected, [])

    def test_lineage_cap_stops_runaway_recovery(self):
        tasks = [
            self._failed("wf-req-challenger"),
            self._failed("wf-req-challenger-r2"),
        ]
        selected = liveness.select_infra_failures_for_recovery(
            tasks, self.REASONS, max_attempts=2
        )
        self.assertEqual(selected, [])

        # 不同谱系互不影响:executor 首次失败仍可补派。
        tasks.append(self._failed("wf-req-executor"))
        selected = liveness.select_infra_failures_for_recovery(
            tasks, self.REASONS, max_attempts=2
        )
        self.assertEqual([t["task_id"] for t in selected], ["wf-req-executor"])

    def test_already_superseded_failure_is_skipped(self):
        failed = self._failed("wf-req-challenger", superseded_by="wf-req-challenger-r2")
        selected = liveness.select_infra_failures_for_recovery(
            [failed], self.REASONS
        )
        self.assertEqual(selected, [])


class ControllerAutoRecoverTest(unittest.TestCase):
    """Controller-side wiring: supersede infra failure + clear stage lock."""

    def _controller(self):
        return importlib.import_module("services.herdr-controller")

    def _tasks(self):
        return [
            {
                "task_id": "wf-req-executor",
                "status": "cleaned",
                "node": "requirements",
                "stage": "requirements",
                "workflow_id": "wf-x",
                "created_at": 1,
                "status_history": [],
            },
            {
                "task_id": "wf-req-challenger",
                "status": "failed",
                "node": "requirements",
                "stage": "requirements",
                "workflow_id": "wf-x",
                "created_at": 2,
                "status_history": [
                    {"to": "failed", "reason": "dispatch_delivery_fuse"}
                ],
            },
        ]

    def test_infra_failure_is_superseded_and_lock_cleared(self):
        controller = self._controller()
        fake_run = unittest.mock.MagicMock(
            return_value=subprocess.CompletedProcess([], 0, "ok", "")
        )
        with patch.object(controller, "load_tasks", return_value=self._tasks()), \
             patch.object(controller, "workflow_closed", return_value=False), \
             patch.object(controller, "_workflow_entry", return_value={"status": "running"}), \
             patch.object(controller, "clear_stage_advance") as clear_lock, \
             patch.object(controller.subprocess, "run", fake_run):
            recovered = controller.recover_infra_failed_tasks("wf-x")

        self.assertTrue(recovered)
        fake_run.assert_called_once_with(
            [
                controller.TASK_MANAGER, "supersede", "wf-req-challenger",
                "--reason", "auto-recover: infrastructure failure",
            ],
            text=True,
            capture_output=True,
        )
        clear_lock.assert_called_once_with("wf-x", "requirements")

    def test_quality_failure_is_left_untouched(self):
        controller = self._controller()
        tasks = self._tasks()
        tasks[1]["status_history"] = [{"to": "failed", "reason": "cli_set_status"}]

        with patch.object(controller, "load_tasks", return_value=tasks), \
             patch.object(controller, "workflow_closed", return_value=False), \
             patch.object(controller, "_workflow_entry", return_value={"status": "running"}), \
             patch.object(controller.subprocess, "run") as run_mock, \
             patch.object(controller, "clear_stage_advance") as clear_lock:
            recovered = controller.recover_infra_failed_tasks("wf-x")

        self.assertFalse(recovered)
        run_mock.assert_not_called()
        clear_lock.assert_not_called()

    def test_closed_workflow_is_skipped(self):
        controller = self._controller()
        with patch.object(controller, "workflow_closed", return_value=True), \
             patch.object(controller.subprocess, "run") as run_mock:
            recovered = controller.recover_infra_failed_tasks("wf-x")
        self.assertFalse(recovered)
        run_mock.assert_not_called()


class SentinelDispatchFuseTest(unittest.TestCase):
    def _sentinel(self):
        return importlib.import_module("services.herdr-sentinel")

    def _breached_task(self, age=3600):
        now = time.time()
        return _task("fuse-1", "dispatched", now - age)

    def test_dead_pane_breach_fails_task_and_notifies(self):
        sentinel = self._sentinel()
        state = {}
        evidence = {"has_marker": False, "agent_status": "idle"}

        with patch.object(sentinel.liveness, "dispatch_delivery_sla", return_value=600), \
             patch.object(sentinel, "_pane_delivery_evidence", return_value=evidence), \
             patch.object(sentinel, "update_statuses", return_value=True) as transitions, \
             patch.object(sentinel, "notify_dispatch_fuse") as notify:
            sentinel.check_dispatch_fuse([self._breached_task()], state)

        transitions.assert_called_once_with(
            {"fuse-1": ("failed", "dispatch_delivery_fuse")}
        )
        notify.assert_called_once()
        self.assertIn("fuse-1", state["dispatch_fuse"])

    def test_live_pane_breach_notifies_without_failing(self):
        sentinel = self._sentinel()
        state = {}
        evidence = {"has_marker": True, "agent_status": "idle"}

        with patch.object(sentinel.liveness, "dispatch_delivery_sla", return_value=600), \
             patch.object(sentinel, "_pane_delivery_evidence", return_value=evidence), \
             patch.object(sentinel, "update_statuses") as transitions, \
             patch.object(sentinel, "notify_dispatch_fuse") as notify:
            sentinel.check_dispatch_fuse([self._breached_task()], state)

        transitions.assert_not_called()
        notify.assert_called_once()

    def test_fuse_disabled_by_env_does_nothing(self):
        sentinel = self._sentinel()
        state = {}

        with patch.object(sentinel.liveness, "dispatch_delivery_sla", return_value=600), \
             patch.dict("os.environ", {"HERDR_DISPATCH_FUSE": "0"}), \
             patch.object(sentinel, "update_statuses") as transitions, \
             patch.object(sentinel, "notify_dispatch_fuse") as notify:
            sentinel.check_dispatch_fuse([self._breached_task()], state)

        transitions.assert_not_called()
        notify.assert_not_called()
        self.assertNotIn("dispatch_fuse", state)

    def test_healthy_tasks_untouched(self):
        sentinel = self._sentinel()
        state = {}
        now = time.time()
        tasks = [_task("ok-1", "dispatched", now - 5)]

        with patch.object(sentinel.liveness, "dispatch_delivery_sla", return_value=600), \
             patch.object(sentinel, "update_statuses") as transitions, \
             patch.object(sentinel, "notify_dispatch_fuse") as notify:
            sentinel.check_dispatch_fuse(tasks, state)

        transitions.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
