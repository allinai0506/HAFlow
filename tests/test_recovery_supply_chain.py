"""Recovery supply-chain regressions from wf-haflow-1022-01 (2026-10-23/24).

Four chained failure modes kept a one-day workflow from finishing:

A. auto-recover superseded a fuse-killed fix task, but no dispatch path picked
   the node up again (the scheduler already considered the node complete; the
   fix-loop recovery sat in waiting_human) — an ~8h silent stall with zero
   events. INV-RECOVERY-02: every auto-supersede must be verified to produce a
   successor within SLA, else escalate exactly once.
B. the stage isolation budget was spent by zero-work rows (router-refused,
   quota-blocked, fuse-killed), exhausting the agent pool fail-closed.
   INV-ROUTER-07: only accepted work spends isolation budget.
C. the sentinel hard-timeout watchdog's only liveness signal was the task
   row's ``updated_at``, so silently-working agents were killed mid-run.
   INV-SENTINEL-04: a working agent gets alert-then-extended-grace, not an
   immediate kill.
D. waiting_human recovery ops produced zero notifications all night.
   INV-RECOVERY-03: waiting_human is audible once per operation version.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr.liveness import EpisodeStore
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_failed_task(store: SQLiteStateStore, task_id: str, *, workflow_id="wf-arc"):
    store.save_workflow({"workflow_id": workflow_id, "status": "running"})
    store.save_task(
        {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "agy",
            "status": "working",
            "pane_id": "p1",
            "created_at": 100.0,
            "started_at": 100.0,
        }
    )
    task = store.get_task(task_id)
    store.compare_and_set_task_transition(
        task_id=task_id,
        to_status="failed",
        reason="dispatch_delivery_fuse",
        source="test",
        expected_status=task["status"],
        expected_version=task["version"],
    )
    return store.get_task(task_id)


class AutoRecoverContractTest(unittest.TestCase):
    """INV-RECOVERY-02: supersede arms a redispatch deadline; silence escalates."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.controller = load_script(
            "controller_arc_test", "services/herdr-controller.py"
        )
        self.clock = [10_000.0]
        self.notifications = []
        self.supersede_outputs = []

        task = seed_failed_task(self.store, "wf-arc-impl-r9")

        def fake_run(command, *args, **kwargs):
            self.supersede_outputs.append(list(command))
            current = self.store.get_task(task["task_id"])
            if current["status"] != "superseded":
                self.store.compare_and_set_task_transition(
                    task_id=task["task_id"],
                    to_status="superseded",
                    reason="auto-recover: infrastructure failure",
                    source="test",
                    expected_status=current["status"],
                    expected_version=current["version"],
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for name, value in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "get_task": self.store.get_task,
            "workflow_closed": lambda _: False,
            "_workflow_entry": lambda _: {"status": "running"},
            "clear_stage_advance": lambda *a, **k: None,
            "_attention_store": EpisodeStore(self.tmp_dir / "attention.json"),
            "notify_attention": lambda title, task, message, reason: (
                self.notifications.append((title, reason)) or True
            ),
            "time": SimpleNamespace(time=lambda: self.clock[0]),
            # 只替换命名空间里的 subprocess 引用;直接改全局 subprocess.run
            # 会污染整个测试进程。
            "subprocess": SimpleNamespace(run=fake_run),
        }.items():
            setattr(self.controller, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def test_supersede_arms_episode_and_contract_closes_on_successor(self):
        controller = self.controller
        assert controller.recover_infra_failed_tasks("wf-arc", self.store.list_tasks())
        key = "wf-arc:auto_recover:wf-arc-impl-r9"
        episode = controller._attention_store.get(key)
        assert episode is not None
        assert episode["reason"] == "auto_recover_awaiting_redispatch"
        assert episode["next_retry_at"] > self.clock[0]

        # A successor binds the superseded task: contract resolved, episode gone.
        task = self.store.get_task("wf-arc-impl-r9")
        self.store.save_task({**task, "superseded_by": "wf-arc-impl-r10"})
        assert not controller.verify_auto_recover_redispatch(
            "wf-arc", self.store.list_tasks()
        )
        assert controller._attention_store.get(key) is None
        assert self.notifications == []

    def test_stalled_redispatch_escalates_exactly_once(self):
        controller = self.controller
        assert controller.recover_infra_failed_tasks("wf-arc", self.store.list_tasks())
        key = "wf-arc:auto_recover:wf-arc-impl-r9"

        # Before the SLA deadline: silent.
        assert not controller.verify_auto_recover_redispatch(
            "wf-arc", self.store.list_tasks()
        )
        assert self.notifications == []

        # Past the deadline with no successor: exactly one escalation.
        self.clock[0] += 400.0
        assert controller.verify_auto_recover_redispatch(
            "wf-arc", self.store.list_tasks()
        )
        assert len(self.notifications) == 1
        assert self.notifications[0][1] == "auto_recover_redispatch_stalled"
        events = self.store.list_events(
            event_type="auto_recover_redispatch_stalled"
        )
        assert len(events) == 1
        assert controller._attention_store.get(key)["escalated"] is True

        # Repeated sweeps do not spam.
        assert not controller.verify_auto_recover_redispatch(
            "wf-arc", self.store.list_tasks()
        )
        assert len(self.notifications) == 1
        assert len(self.store.list_events(event_type="auto_recover_redispatch_stalled")) == 1

    def test_illegal_transition_from_parallel_supersede_is_idempotent(self):
        controller = self.controller

        def fake_run(command, *args, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Illegal transition: superseded -> superseded"
            )

        controller.subprocess = SimpleNamespace(run=fake_run)
        # Parallel actor already superseded the task.
        task = self.store.get_task("wf-arc-impl-r9")
        self.store.compare_and_set_task_transition(
            task_id="wf-arc-impl-r9",
            to_status="superseded",
            reason="external",
            source="test",
            expected_status=task["status"],
            expected_version=task["version"],
        )
        # Selector no longer picks it (status superseded); nothing errors.
        assert not controller.recover_infra_failed_tasks(
            "wf-arc", self.store.list_tasks()
        )


class RouterZeroWorkExemptionTest(unittest.TestCase):
    """INV-ROUTER-07: zero-work terminal rows do not spend isolation budget."""

    def setUp(self):
        import tempfile

        from herdr.state_store import get_state_store

        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"
        self.store = get_state_store(self.db_path)
        self.wf_id = "wf-zero-work"
        self.store.save_workflow(
            {
                "workflow_id": self.wf_id,
                "project_id": "test-proj",
                "status": "running",
                "healthy_agents": ["qodercli", "claude"],
                "unhealthy_agents": {},
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _choose(self, node_cfg):
        from herdr import agent_router

        with patch("herdr.agent_router._get_store", return_value=self.store), patch(
            "herdr.agent_router.workflow_config_for",
            return_value={"nodes": [node_cfg]},
        ):
            return agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")

    def test_zero_work_failed_row_does_not_spend_budget(self):
        self.store.save_task(
            {
                "task_id": "impl-r8",
                "workflow_id": self.wf_id,
                "node": "implementation",
                "stage": "implementation",
                "agent": "qodercli",
                "status": "failed",
                "failure_reason": "router_isolation_rejected",
            }
        )
        chosen = self._choose(
            {
                "id": "test",
                "agent_policy": {
                    "preferred": ["qodercli", "claude"],
                    "exclude_stage_agents": ["implementation"],
                },
            }
        )
        self.assertEqual(chosen, "qodercli")

    def test_accepted_work_still_spends_budget(self):
        self.store.save_task(
            {
                "task_id": "impl-r2",
                "workflow_id": self.wf_id,
                "node": "implementation",
                "stage": "implementation",
                "agent": "qodercli",
                "status": "superseded",
                "commit": "a" * 40,
            }
        )
        chosen = self._choose(
            {
                "id": "test",
                "agent_policy": {
                    "preferred": ["qodercli", "claude"],
                    "exclude_stage_agents": ["implementation"],
                },
            }
        )
        self.assertEqual(chosen, "claude")

    def test_inflight_row_still_spends_budget(self):
        self.store.save_task(
            {
                "task_id": "impl-live",
                "workflow_id": self.wf_id,
                "node": "implementation",
                "stage": "implementation",
                "agent": "qodercli",
                "status": "working",
            }
        )
        chosen = self._choose(
            {
                "id": "test",
                "agent_policy": {
                    "preferred": ["qodercli", "claude"],
                    "exclude_stage_agents": ["implementation"],
                },
            }
        )
        self.assertEqual(chosen, "claude")

    def test_kill_switch_restores_fail_closed(self):
        self.store.save_task(
            {
                "task_id": "impl-r8",
                "workflow_id": self.wf_id,
                "node": "implementation",
                "stage": "implementation",
                "agent": "qodercli",
                "status": "failed",
                "failure_reason": "router_isolation_rejected",
            }
        )
        with patch.dict(
            os.environ, {"HERDR_ROUTER_ISOLATION_ZERO_WORK_EXEMPT": "0"}
        ):
            chosen = self._choose(
                {
                    "id": "test",
                    "agent_policy": {
                        "preferred": ["qodercli", "claude"],
                        "exclude_stage_agents": ["implementation"],
                    },
                }
            )
        self.assertEqual(chosen, "claude")


class SentinelAliveGraceTest(unittest.TestCase):
    """INV-SENTINEL-04: working agents get alert + extended grace, not instant kill."""

    def setUp(self):
        self.sentinel = importlib.import_module("services.herdr-sentinel")

    def _task(self, now, idle):
        return {
            "task_id": "wf-alive-impl-r1",
            "workflow_id": "wf-alive",
            "node": "implementation",
            "stage": "implementation",
            "status": "working",
            "pane_id": "p-alive",
            "updated_at": now - idle,
            "version": 2,
        }

    def test_alive_agent_alerts_instead_of_failing(self):
        now = 100_000.0
        task = self._task(now, 4000)
        state = {"stalls": {}, "timeouts": {}}
        notified = []

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "working"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: notified.append(a), now=now
            )

        fail_mock.assert_not_called()
        self.assertTrue(state["timeouts"][task["task_id"]]["alive_alerted"])
        alive_alerts = [n for n in notified if "看门狗宽限" in n[2]]
        self.assertEqual(len(alive_alerts), 1)

        # Repeated sweeps do not re-alert.
        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "working"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: notified.append(a), now=now + 60
            )
        fail_mock.assert_not_called()
        alive_alerts = [n for n in notified if "看门狗宽限" in n[2]]
        self.assertEqual(len(alive_alerts), 1)

    def test_alive_agent_fails_beyond_extended_threshold(self):
        now = 100_000.0
        task = self._task(now, 8000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "working"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: None, now=now
            )

        fail_mock.assert_called_once()

    def test_dead_agent_fails_at_hard_timeout(self):
        now = 100_000.0
        task = self._task(now, 4000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: None, now=now
            )

        fail_mock.assert_called_once()

    def test_multiplier_one_restores_immediate_kill(self):
        now = 100_000.0
        task = self._task(now, 4000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.dict(
            os.environ, {"HERDR_TASK_HARD_TIMEOUT_ALIVE_MULTIPLIER": "1"}
        ), patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "working"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: None, now=now
            )

        fail_mock.assert_called_once()


class WaitingHumanNotifyTest(unittest.TestCase):
    """INV-RECOVERY-03: waiting_human operations notify once per version."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.controller = load_script(
            "controller_whn_test", "services/herdr-controller.py"
        )
        self.clock = [10_000.0]
        self.notifications = []
        for name, value in {
            "_get_store": lambda: self.store,
            "reconcile_node_dispatches": lambda *a, **k: None,
            "_attention_store": EpisodeStore(self.tmp_dir / "attention.json"),
            "notify_attention": lambda title, task, message, reason: (
                self.notifications.append((title, reason)) or True
            ),
            "time": SimpleNamespace(time=lambda: self.clock[0]),
        }.items():
            setattr(self.controller, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def _with_ops(self, ops):
        from herdr import workflow_recovery as wr

        with patch.object(
            wr,
            "drive_recovery",
            lambda store, workflow_id, execute, now=None: ops,
        ):
            self.controller.check_workflow_recovery("wf-whn")

    def test_notify_once_per_version_and_clear_on_resolution(self):
        op = {
            "id": 252949,
            "status": "waiting_human",
            "version": 5,
            "detail": {"reason": "identity_unknown", "decision_needed": "use recovery-decide"},
            "payload": {"kind": "fix_loop", "task_ids": ["wf-whn-impl-r7"]},
        }
        self._with_ops([op])
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.notifications[0][1], "recovery_waiting_human")

        # Same version: dedup.
        self._with_ops([op])
        self.assertEqual(len(self.notifications), 1)

        # New version after a decision: notify again.
        self._with_ops([dict(op, version=6)])
        self.assertEqual(len(self.notifications), 2)

        # Op resolved: episode cleared, no more notifications.
        self._with_ops([dict(op, version=6, status="superseded")])
        self.assertEqual(len(self.notifications), 2)
        assert self.controller._attention_store.get("wf-whn:recovery_waiting:252949") is None


class RecoveryScanDrivesContractTest(unittest.TestCase):
    """check_workflow_recovery 必须持续核对补派契约(wf-haflow-1022-01 断链点)."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.controller = load_script(
            "controller_scan_test", "services/herdr-controller.py"
        )
        self.clock = [10_000.0]
        self.notifications = []
        for name, value in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "get_task": self.store.get_task,
            "workflow_closed": lambda _: False,
            "_workflow_entry": lambda _: {"status": "running"},
            "reconcile_node_dispatches": lambda *a, **k: None,
            "_attention_store": EpisodeStore(self.tmp_dir / "attention.json"),
            "notify_attention": lambda title, task, message, reason: (
                self.notifications.append((title, reason)) or True
            ),
            "time": SimpleNamespace(time=lambda: self.clock[0]),
            "clear_stage_advance": lambda *a, **k: None,
        }.items():
            setattr(self.controller, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_verifies_armed_auto_recover_beyond_deadline(self):
        seed_failed_task(self.store, "wf-arc-impl-r9", workflow_id="wf-arc")
        task = self.store.get_task("wf-arc-impl-r9")
        self.store.compare_and_set_task_transition(
            task_id="wf-arc-impl-r9",
            to_status="superseded",
            reason="auto-recover: infrastructure failure",
            source="test",
            expected_status=task["status"],
            expected_version=task["version"],
        )
        self.controller._attention_store.upsert(
            "wf-arc:auto_recover:wf-arc-impl-r9",
            {
                "task_id": "wf-arc-impl-r9",
                "workflow_id": "wf-arc",
                "node": "implementation",
                "reason": "auto_recover_awaiting_redispatch",
                "next_retry_at": self.clock[0] - 1.0,
            },
        )
        from herdr import workflow_recovery as wr

        with patch.object(
            wr, "drive_recovery", lambda store, workflow_id, execute, now=None: []
        ):
            self.controller.check_workflow_recovery("wf-arc")

        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.notifications[0][1], "auto_recover_redispatch_stalled")
        events = self.store.list_events(event_type="auto_recover_redispatch_stalled")
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
