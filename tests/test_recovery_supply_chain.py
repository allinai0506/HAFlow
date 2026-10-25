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


# ---------------------------------------------------------------------------
# Chain F (wf-haflow-1022-01 深层链): launch-refused rows must not poison
# fix-loop identity, and a failed predecessor roots its successor at the
# frozen candidate instead of dead-ending in waiting_human.
# ---------------------------------------------------------------------------

class LaunchRefusalExclusionTest(unittest.TestCase):
    """INV-RECOVERY-04: 零资源拒绝行不是执行事实,不入 fix_loop 身份校验。"""

    SHA = "a" * 40

    def _workflow(self):
        return {
            "workflow_id": "wf-f",
            "execution_id": "exec-1",
            "candidate_sha": self.SHA,
            "status": "running",
            "startup_ready": True,
        }

    def _config(self):
        return {"nodes": [{"id": "implementation"}, {"id": "test", "gate": {"retry_node": "implementation"}}]}

    def _tasks(self, refused_reason="router_isolation_rejected"):
        killed = {
            "task_id": "t-r7", "workflow_id": "wf-f", "node": "test", "stage": "test",
            "status": "failed", "failure_reason": "hard_timeout_watchdog",
            "blocker": "watchdog killed before commit", "run_id": "r7-run",
            "execution_id": "exec-1", "candidate_sha": self.SHA,
        }
        refused = {
            "task_id": "t-r8", "workflow_id": "wf-f", "node": "test", "stage": "test",
            "status": "failed", "failure_reason": refused_reason,
            "blocker": "No available Agent", "run_id": None,
            "execution_id": None, "candidate_sha": self.SHA,
        }
        return [killed, refused]

    def test_refused_row_excluded_from_identity_facts(self):
        from herdr.workflow_progress import assess_workflow

        result = assess_workflow(self._workflow(), self._config(), self._tasks())
        obligations = result["obligations"]
        self.assertTrue(obligations)
        self.assertNotIn("t-r8", obligations[0]["task_ids"])
        self.assertNotEqual(obligations[0]["reason"], "identity_unknown")

    def test_non_refusal_zero_identity_row_still_poisons(self):
        """反例:普通失败行缺 execution_id 仍判 identity_unknown(闸门仍在)。"""
        from herdr.workflow_progress import assess_workflow

        result = assess_workflow(
            self._workflow(), self._config(), self._tasks(refused_reason="cli_set_status")
        )
        obligations = result["obligations"]
        self.assertTrue(obligations)
        self.assertEqual(obligations[0]["reason"], "identity_unknown")


class SuccessorRootShaTest(unittest.TestCase):
    """INV-RECOVERY-05: 无 commit 前驱的继任者扎根冻结候选。"""

    SHA = "b" * 40

    def test_root_sha_overrides_predecessor_commit(self):
        from herdr.workflow_recovery import successor_launch_command

        workflow = {"workflow_id": "wf-x", "execution_id": "exec"}
        predecessor = {
            "task_id": "old", "node": "implementation", "stage": "implementation",
            "dispatch_role": "worker", "dispatch_round": 3,
        }
        argv = successor_launch_command(
            "herdr-task", workflow, predecessor, "new", "/src/repo", "prompt",
            root_sha=self.SHA,
        )
        self.assertIn("--candidate-sha", argv)
        self.assertEqual(argv[argv.index("--candidate-sha") + 1], self.SHA)
        self.assertEqual(argv[argv.index("--supersedes") + 1], "old")

    def test_without_commit_or_root_still_fails_closed(self):
        from herdr.workflow_recovery import successor_launch_command

        with self.assertRaises(ValueError):
            successor_launch_command(
                "herdr-task", {"workflow_id": "wf", "execution_id": "e"},
                {"task_id": "old", "node": "implementation"}, "new", "/src", "p",
            )


class FrozenCandidateSuccessorExecutionTest(unittest.TestCase):
    """执行层:failed 前驱自动构造继任者而不是 target_requires_explicit_replacement。"""

    def setUp(self):
        import subprocess as sp
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.repo = self.tmp_dir / "repo"
        self.repo.mkdir()
        for argv in (["init", "-q", "-b", "main"], ["config", "user.name", "t"],
                     ["config", "user.email", "t@e.invalid"]):
            sp.run(["git", "-C", str(self.repo), *argv], check=True, capture_output=True)
        (self.repo / "seed").write_text("x")
        sp.run(["git", "-C", str(self.repo), "add", "."], check=True, capture_output=True)
        sp.run(["git", "-C", str(self.repo), "commit", "-qm", "seed", "--no-gpg-sign"],
               check=True, capture_output=True)
        self.sha = sp.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        config = {"nodes": [
            {"id": "implementation"},
            {"id": "test", "gate": {"retry_node": "implementation"}},
        ]}
        self.store.save_workflow({
            "workflow_id": "wf-fc", "status": "running", "execution_id": "exec-fc",
            "project_root": str(self.repo), "base_branch": "main", "config": config,
            "candidate_sha": self.sha, "startup_ready": True,
        })
        self.store.record_event(
            "candidate_frozen", {"candidate_sha": self.sha},
            workflow_id="wf-fc", source="critical-path-scheduler",
        )
        self.store.save_task({
            "task_id": "old", "workflow_id": "wf-fc", "node": "implementation",
            "stage": "implementation", "status": "failed",
            "failure_reason": "hard_timeout_watchdog", "blocker": "killed pre-commit",
            "run_id": "old-run", "execution_id": "exec-fc", "candidate_sha": self.sha,
            "dispatch_role": "worker", "dispatch_round": 3,
        })
        self.store.save_task({
            "task_id": "gate", "workflow_id": "wf-fc", "node": "test",
            "stage": "test", "status": "cleaned", "run_id": "gate-run",
            "execution_id": "exec-fc", "candidate_sha": self.sha,
            "stage_verdict": "blocked",
            "stage_verdict_affected_task_ids": ["old"],
        })

        self.controller = load_script(
            "controller_fc_test", "services/herdr-controller.py"
        )
        self.launch_commands = []

        def fake_launch(command, *args, **kwargs):
            argv = list(map(str, command))
            self.launch_commands.append(argv)
            successor_id = argv[argv.index("--task-id") + 1]
            run_id = "new-run"
            self.store.save_task({
                "task_id": successor_id, "workflow_id": "wf-fc",
                "node": "implementation", "stage": "implementation",
                "status": "dispatched", "run_id": run_id,
                "execution_id": "exec-fc", "candidate_sha": self.sha,
                "dispatch_role": "worker", "dispatch_round": 4,
                "completion_identity_path": f"/tmp/id-{successor_id}",
                "completion_epoch": "epoch-1",
                "supersedes": "old",
            })
            self.store.record_event(
                "initial_dispatched",
                {
                    "task_id": successor_id, "delivery_phase": "dispatched",
                    "completion_epoch": "epoch-1",
                    "identity_path": f"/tmp/id-{successor_id}",
                    "intervention_id": "initial:test",
                },
                workflow_id="wf-fc", task_id=successor_id, node_id="implementation",
                source="herdr-task", run_id=run_id,
            )
            old = self.store.get_task("old")
            self.store.save_task({**old, "superseded_by": successor_id})
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for name, value in {
            "_get_store": lambda: self.store,
            "subprocess": SimpleNamespace(run=fake_launch),
            "clear_stage_advance": lambda *a, **k: None,
            "invalidate_for_fix_loop": self._invalidate,
        }.items():
            setattr(self.controller, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def _invalidate(self, wid, gate, config, retry_node=None, preserve_task_ids=None, **kwargs):
        """Match the real invalidator's guarded supersede; assertions test successor construction."""
        task = self.store.get_task("gate")
        if task:
            if kwargs.get("before_effect"):
                kwargs["before_effect"]()
            result = self.store.compare_and_set_task_transition(
                task_id=task["task_id"], to_status="superseded",
                expected_status=task["status"], expected_version=task["version"],
                reason="fix-loop gate invalidation", source="test",
            )
            assert result["accepted"]

    def _claim_operation(self):
        import time as _time

        from herdr import recovery_store

        now = _time.time() + 1
        for op in recovery_store.list_operations(self.store.db_path, "wf-fc"):
            payload = op.get("payload") or {}
            if (payload.get("kind") == "fix_loop"
                    and payload.get("affected_task_ids") == ["old"]):
                assert op["status"] == "pending", f"assessed op not pending: {op['status']} {payload}"
                claimed = recovery_store.claim_operation(
                    self.store.db_path, op["id"], "owner", now, lease_seconds=600
                )
                assert claimed is not None, "claim failed"
                return dict(claimed, detail={})
        raise AssertionError("fix_loop obligation for 'old' was not registered")

    def test_failed_predecessor_gets_frozen_rooted_successor(self):
        operation = self._claim_operation()
        status, detail = self.controller.execute_workflow_recovery(operation, "owner")
        self.assertEqual(status, "awaiting_result")
        self.assertEqual(detail["repair_map"]["old"]["root"], "frozen_candidate")
        argv = self.launch_commands[0]
        self.assertEqual(argv[argv.index("--candidate-sha") + 1], self.sha)
        self.assertEqual(argv[argv.index("--supersedes") + 1], "old")
        self.assertEqual(self.store.get_task("old")["superseded_by"], argv[argv.index("--task-id") + 1])

class BaselineStaleNotifyTest(unittest.TestCase):
    """链E最小切片:baseline_stale 拒绝必须立刻通知,不再静默躺死行。"""

    def test_refusal_notifies_with_remediation(self):
        from types import SimpleNamespace as NS

        import importlib as _il
        notifier_module = _il.import_module("services.herdr-notifier")
        from herdr import launch_baseline, task_cli_launch_preflight as preflight

        args = NS(
            workflow_id="wf-e", task_id="wf-e-impl-r3", node="implementation",
            stage="implementation", candidate_sha="c" * 40, onto=None,
        )
        events = []
        notifications = []

        class Store:
            def record_event(self, event_type, payload, **kwargs):
                events.append(event_type)
                return {"id": 1}

        probe = {
            "status": "stale", "dev_tip": "d" * 40, "claim_sha": "c" * 40,
            "behind_by": 7, "dev_ref": "origin/dev", "fetched": True, "detail": "",
        }
        with patch.object(
            launch_baseline, "probe_claim_freshness", return_value=probe
        ), patch.object(
            notifier_module, "notify",
            lambda title, subtitle, message, url=None: notifications.append(message) or True,
        ), patch.object(
            notifier_module, "build_console_url", lambda **k: "http://console"
        ):
            # importlib.import_module 在函数内解析 notifier;用 sys.modules 预置同一对象
            import sys as _sys
            _sys.modules.setdefault("services.herdr-notifier", notifier_module)
            with self.assertRaises(SystemExit) as ctx:
                preflight.preflight_baseline_freshness(
                    args, "/src/repo", "dev",
                    get_store=lambda: Store(), record_router_failure=lambda *a, **k: None,
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(events, ["baseline_stale_refused"])
        self.assertEqual(len(notifications), 1)
        self.assertIn("rebase", notifications[0])


# ---------------------------------------------------------------------------
# Dead-row convergence batch (wf-haflow-1022-01 wrapup 段缺陷目录):
# D1 half-written failure identity, D2 post-dispatch back-link containment,
# D3 pinned-agent router recovery, G1 dangling obligation convergence,
# G2 baseline_stale sink exemption.
# ---------------------------------------------------------------------------

class RecordRouterFailureIdentityTest(unittest.TestCase):
    """D1/INV-REG-01: 失败登记行身份必须与意图同样完整。"""

    def _record(self, tmp_path, *, with_intent=False):
        import time

        from herdr.state_store import SQLiteStateStore
        from herdr.task_cli_launch import record_router_failure_task

        store = SQLiteStateStore(tmp_path / "state.db")
        args = SimpleNamespace(
            task_id="t-fail", workflow_id="wf-d1", node="test", stage="test",
            agent="qodercli", integration_mode="none", goal="g", acceptance=[],
            execution_id="exec-d1", run_id=None,
        )
        if with_intent:
            from herdr import task_resources as tr

            key = tr.dispatch_identity("wf-d1", "test", "worker", "c" * 40, 3, "exec-d1")
            args._launch_intent = {
                "role": "worker", "dispatch_round": 3, "candidate_sha": "c" * 40,
                "intent_id": "intent-1", "supersedes": None,
                "execution_id": "exec-d1", "task_id": "t-fail",
                "key": key, "workflow_id": "wf-d1", "node_id": "test",
                "phase": "allocating", "lease_until": time.time() + 60,
                "resources": {},
            }
            # 真实流程中 claim 先落 launch_intent 事件;补齐以还原现场。
            from herdr.node_dispatch_store import record_intent

            record_intent(store.db_path, args._launch_intent, time.time())
        record_router_failure_task(
            args, "test", "no eligible agent",
            get_store=lambda: store, new_run_id=lambda: "run-d1",
            now=time.time, release_agent_reservation=lambda *_: None,
        )
        return store

    def test_failure_row_carries_execution_id(self):
        import tempfile
        from pathlib import Path as P

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = self._record(P(tmp.name))
        row = store.get_task("t-fail")
        self.assertEqual(row.get("execution_id"), "exec-d1")

    def test_intent_execution_id_wins_and_supersede_then_succeeds(self):
        """D1+D2 集成:身份完整的失败行可以被正规 supersede 回链。"""
        import tempfile
        from pathlib import Path as P

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = P(tmp.name)
        store = self._record(root, with_intent=True)
        row = store.get_task("t-fail")
        self.assertEqual(row.get("execution_id"), "exec-d1")
        # 保存路径不再被登记身份护栏拒绝:update 后行仍可写。
        store.save_task({**row, "blocker": "updated"})
        self.assertEqual(store.get_task("t-fail")["blocker"], "updated")


class RouterRecoveryAutoFallbackTest(unittest.TestCase):
    """D3/INV-ROUTER-08: 原指定 agent 仍被隔离时回退 auto 再判池。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.store.save_workflow({"workflow_id": "wf-d3", "status": "running"})
        self.store.save_task({
            "task_id": "t-r5", "workflow_id": "wf-d3", "node": "test",
            "stage": "test", "agent": "qodercli", "status": "failed",
            "failure_reason": "router_isolation_rejected", "task_type": "test",
        })
        self.controller = load_script("controller_d3_test", "services/herdr-controller.py")
        self.commands = []

        def fake_run(command, *a, **k):
            self.commands.append(list(map(str, command)))
            task = self.store.get_task("t-r5")
            self.store.save_task({**task, "status": "superseded",
                                  "superseded_by": "t-r6", "replacement_pending": False})
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for name, value in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "get_task": self.store.get_task,
            "workflow_closed": lambda _: False,
            "subprocess": SimpleNamespace(run=fake_run),
        }.items():
            setattr(self.controller, name, value)

    def test_falls_back_to_auto_and_supersedes(self):
        import herdr.agent_router as router

        calls = []

        def fake_choose(workflow_id, stage, task_type, requested="auto", **kw):
            calls.append(requested)
            if requested == "qodercli":
                raise router.RouterIsolationRejection("used in stage")
            return "claude"

        with patch.object(router, "choose_agent", fake_choose):
            # infra_recovery imports choose_agent inside the function from
            # herdr.agent_router, so patching the module attribute suffices.
            recovered = self.controller.recover_router_isolation_tasks("wf-d3")
        self.assertTrue(recovered)
        self.assertEqual(calls, ["qodercli", "auto"])
        self.assertEqual(self.store.get_task("t-r5")["status"], "superseded")

    def test_no_fallback_when_auto_also_rejected(self):
        import herdr.agent_router as router

        def fake_choose(workflow_id, stage, task_type, requested="auto", **kw):
            raise router.RouterIsolationRejection("pool exhausted")

        with patch.object(router, "choose_agent", fake_choose):
            recovered = self.controller.recover_router_isolation_tasks("wf-d3")
        self.assertFalse(recovered)
        self.assertEqual(self.commands, [])


class DanglingObligationConvergenceTest(unittest.TestCase):
    """G1/INV-RECOVERY-06: 悬空替换义务由当前候选上的 settled pass 收敛。"""

    SHA = "e" * 40

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.store.save_workflow({"workflow_id": "wf-g1", "status": "running"})
        # r1: 悬空义务(superseded + pending + 无接替)
        self.store.save_task({
            "task_id": "t-r1", "workflow_id": "wf-g1", "node": "test",
            "stage": "test", "agent": "agy", "status": "superseded",
            "replacement_pending": True, "run_id": "r1-run",
            "execution_id": "exec-g1",
        })
        # r6: 当前冻结候选上的 settled pass
        self.store.save_task({
            "task_id": "t-r6", "workflow_id": "wf-g1", "node": "test",
            "stage": "test", "agent": "qodercli", "status": "cleaned",
            "stage_verdict": "pass", "verified_candidate_sha": self.SHA,
            "candidate_sha": self.SHA, "run_id": "r6-run", "execution_id": "exec-g1",
        })
        self.controller = load_script("controller_g1_test", "services/herdr-controller.py")
        for name, value in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "workflow_closed": lambda _: False,
            "_scheduler_current_frozen_candidate_sha": lambda _: self.SHA,
        }.items():
            setattr(self.controller, name, value)

    def test_dangling_obligation_resolved_by_settled_pass(self):
        self.assertTrue(
            self.controller.converge_dangling_replacement_obligations("wf-g1")
        )
        row = self.store.get_task("t-r1")
        self.assertNotEqual(row.get("replacement_pending"), True)
        events = self.store.list_events(event_type="replacement_obligation_auto_resolved")
        self.assertEqual(len(events), 1)
        # 幂等:再次运行无事可做。
        self.assertFalse(
            self.controller.converge_dangling_replacement_obligations("wf-g1")
        )

    def test_no_settled_pass_no_convergence(self):
        self.store.save_task({
            "task_id": "t-r6", "workflow_id": "wf-g1", "node": "test",
            "stage": "test", "agent": "qodercli", "status": "cleaned",
            "stage_verdict": "pass", "verified_candidate_sha": "f" * 40,
            "candidate_sha": "f" * 40, "run_id": "r6-run", "execution_id": "exec-g1",
        })
        self.assertFalse(
            self.controller.converge_dangling_replacement_obligations("wf-g1")
        )
        self.assertTrue(self.store.get_task("t-r1").get("replacement_pending"))

    def test_check_workflow_recovery_drives_convergence(self):
        from herdr import workflow_recovery as wr

        with patch.object(
            wr, "drive_recovery", lambda store, workflow_id, execute, now=None: []
        ):
            self.controller.check_workflow_recovery("wf-g1")
        self.assertNotEqual(self.store.get_task("t-r1").get("replacement_pending"), True)


class BaselineStaleSinkExemptionTest(unittest.TestCase):
    """G2/INV-BASELINE-02: 无下游消费者的非 git 汇点不做 stale 拒绝。"""

    def _args(self, node="wrapup", integration="none"):
        return SimpleNamespace(
            workflow_id="wf-g2", node=node, stage=node,
            candidate_sha="c" * 40, onto=None, integration_mode=integration,
        )

    def _probe(self):
        return {
            "status": "stale", "dev_tip": "d" * 40, "claim_sha": "c" * 40,
            "behind_by": 3, "dev_ref": "origin/dev", "fetched": True, "detail": "",
        }

    def test_sink_node_is_exempt(self):
        from herdr import launch_baseline, task_cli_launch_preflight as preflight
        from herdr import projects

        events = []

        class Store:
            def record_event(self, *a, **k):
                events.append(a[0])
                return {"id": 1}

        cfg = {"nodes": [
            {"id": "implementation"}, {"id": "test", "depends_on": ["implementation"]},
            {"id": "review", "depends_on": ["implementation"]},
            {"id": "wrapup", "depends_on": ["test", "review"]},
        ]}
        with patch.object(launch_baseline, "probe_claim_freshness", return_value=self._probe()), \
             patch.object(projects, "workflow_config_for", return_value=cfg):
            result = preflight.preflight_baseline_freshness(
                self._args(), "/src", "dev",
                get_store=lambda: Store(), record_router_failure=lambda *a, **k: None,
            )
        self.assertEqual(result["status"], "stale")  # 返回探测结果但不拒绝
        self.assertEqual(events, [])

    def test_depended_node_still_refused(self):
        import importlib as _il

        notifier_module = _il.import_module("services.herdr-notifier")
        from herdr import launch_baseline, projects
        from herdr import task_cli_launch_preflight as preflight

        events = []

        class Store:
            def record_event(self, *a, **k):
                events.append(a[0])
                return {"id": 1}

        cfg = {"nodes": [
            {"id": "implementation"},
            {"id": "test", "depends_on": ["implementation"]},
            {"id": "wrapup", "depends_on": ["test"]},
        ]}
        with patch.object(launch_baseline, "probe_claim_freshness", return_value=self._probe()), \
             patch.object(projects, "workflow_config_for", return_value=cfg), \
             patch.object(notifier_module, "notify", lambda *a, **k: True), \
             patch.object(notifier_module, "build_console_url", lambda **k: "u"):
            import sys as _sys
            _sys.modules.setdefault("services.herdr-notifier", notifier_module)
            with self.assertRaises(SystemExit) as ctx:
                preflight.preflight_baseline_freshness(
                    self._args(node="test"), "/src", "dev",
                    get_store=lambda: Store(), record_router_failure=lambda *a, **k: None,
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(events, ["baseline_stale_refused"])
