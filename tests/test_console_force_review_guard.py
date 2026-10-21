"""Regression tests for SC-019: Console Force Review Privileged Transitions and CAS Guard.

Verifies:
1. Input validation: task_id empty or missing, invalid expected_version, non-existent task.
2. Permitted state transitions: all non-terminal states ('working', 'blocked', 'paused',
   'dispatched', 'rework') successfully transition to 'agent_done' via 'console-force-review' intent.
3. Terminal state rejection: terminal states ('completed', 'superseded') are rejected fail-closed,
   preventing invalid state machine corruption.
4. Concurrency CAS guard: version mismatch and status mismatch reject stale writes without side-effects.
5. Audit trail: operator identity and review reason are recorded in task_transition event payload and history.
6. Coordinator notification resilience: pane messaging failures do not abort state machine transition.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from herdr.state_store import SQLiteStateStore
from console import task_api


class TestConsoleForceReviewGuard(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self._orig_store = getattr(task_api.herdr_kernel, "get_state_store", None)
        task_api.herdr_kernel.get_state_store = lambda *args, **kwargs: self.store

    def tearDown(self):
        if self._orig_store is not None:
            task_api.herdr_kernel.get_state_store = self._orig_store
        self.td.cleanup()

    def _create_task(self, task_id: str, status: str = "working", version: int = 1, workflow_id: str = "wf-fr-01"):
        now = time.time()
        task = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "run_id": f"run-{workflow_id}",
            "node": "exec",
            "stage": "exec",
            "status": status,
            "agent": "codex",
            "version": version,
            "created_at": now,
            "updated_at": now,
        }
        self.store.save_task(task)
        if version != 1:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE tasks SET version = ? WHERE task_id = ?", (version, task_id))
            conn.commit()
            conn.close()
        return task

    def test_force_review_empty_or_missing_task_id_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({})
        self.assertIn("task_id 不能为空", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({"task_id": "   "})
        self.assertIn("task_id 不能为空", str(ctx.exception))

    def test_force_review_invalid_expected_version_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({"task_id": "t1", "expected_version": "not-an-int"})
        self.assertIn("无效的 expected_version", str(ctx.exception))

    def test_force_review_task_not_found_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({"task_id": "non-existent-task"})
        self.assertIn("未找到任务 non-existent-task", str(ctx.exception))

    def test_force_review_transitions_from_non_terminal_states(self):
        non_terminal_statuses = ["working", "blocked", "paused", "dispatched", "rework"]
        for idx, initial_status in enumerate(non_terminal_statuses):
            tid = f"task-fr-nt-{idx}"
            self._create_task(tid, status=initial_status, version=1, workflow_id=f"wf-fr-{idx}")

            res = task_api.api_task_force_review({"task_id": tid})
            self.assertEqual(res.get("status"), "agent_done")
            self.assertEqual(res.get("task_id"), tid)

            # Verify SQLite task state
            task = self.store.get_task(tid)
            self.assertEqual(task["status"], "agent_done")
            self.assertEqual(task["version"], 2)

            # Verify authoritative task_transition event
            events = self.store.list_events(workflow_id=f"wf-fr-{idx}", event_type="task_transition")
            self.assertEqual(len(events), 1)
            ev = events[0]
            self.assertEqual(ev.get("source"), "console-force-review")
            payload = ev.get("payload") or {}
            self.assertEqual(payload.get("from_status"), initial_status)
            self.assertEqual(payload.get("to_status"), "agent_done")
            self.assertEqual(payload.get("intent"), "console-force-review")

    def test_force_review_rejected_for_terminal_states(self):
        terminal_statuses = ["completed", "superseded"]
        for idx, term_status in enumerate(terminal_statuses):
            tid = f"task-fr-term-{idx}"
            self._create_task(tid, status=term_status, version=1, workflow_id=f"wf-term-{idx}")

            with self.assertRaises(RuntimeError) as ctx:
                task_api.api_task_force_review({"task_id": tid})
            self.assertIn("任务状态流转失败", str(ctx.exception))

            # Verify DB status was NOT mutated
            task = self.store.get_task(tid)
            self.assertEqual(task["status"], term_status)
            self.assertEqual(task["version"], 1)

            # Verify no task_transition event was recorded
            events = self.store.list_events(workflow_id=f"wf-term-{idx}", event_type="task_transition")
            self.assertEqual(len(events), 0)

    def test_force_review_cas_version_mismatch_fails_closed(self):
        tid = "task-fr-cas-ver"
        self._create_task(tid, status="working", version=2, workflow_id="wf-cas-ver")

        # Stale caller thinks version is 1
        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({"task_id": tid, "expected_version": 1})
        self.assertIn("cas_mismatch", str(ctx.exception))

        # Ensure task untouched
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "working")
        self.assertEqual(task["version"], 2)

    def test_force_review_cas_status_mismatch_fails_closed(self):
        tid = "task-fr-cas-st"
        self._create_task(tid, status="working", version=1, workflow_id="wf-cas-st")

        # Caller expects status to be "blocked"
        with self.assertRaises(RuntimeError) as ctx:
            task_api.api_task_force_review({"task_id": tid, "expected_status": "blocked"})
        self.assertIn("cas_mismatch", str(ctx.exception))

        # Ensure task untouched
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "working")
        self.assertEqual(task["version"], 1)

    def test_force_review_operator_and_reason_audit(self):
        tid = "task-fr-audit-01"
        self._create_task(tid, status="working", version=1, workflow_id="wf-audit")

        res = task_api.api_task_force_review({
            "task_id": tid,
            "operator": "lead_engineer_bob",
            "reason": "Emergency bypass review requested by customer",
            "expected_version": 1,
            "expected_status": "working",
        })
        self.assertEqual(res.get("status"), "agent_done")

        # Verify task row reflects operator
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "agent_done")
        self.assertEqual(task["version"], 2)
        self.assertEqual(task.get("operator"), "lead_engineer_bob")

        # Verify task_transition event records operator and reason
        events = self.store.list_events(workflow_id="wf-audit", event_type="task_transition")
        self.assertEqual(len(events), 1)
        ev_payload = events[0].get("payload") or {}
        self.assertEqual(ev_payload.get("operator"), "lead_engineer_bob")
        self.assertEqual(ev_payload.get("reason"), "Emergency bypass review requested by customer")
        self.assertEqual(ev_payload.get("intent"), "console-force-review")

    def test_force_review_coordinator_notification_handling(self):
        tid = "task-fr-notify-01"
        wid = "wf-notify"
        self._create_task(tid, status="working", version=1, workflow_id=wid)
        self.store.save_workflow({
            "workflow_id": wid,
            "title": "Notify Workflow",
            "status": "running",
            "coordinator_pane_id": "%12",
        })

        # Success case
        with patch("console.common.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_run.return_value = mock_proc

            res = task_api.api_task_force_review({"task_id": tid})
            self.assertTrue(res.get("notified"))
            mock_run.assert_called_once()
            called_args = mock_run.call_args[0][0]
            self.assertIn("HERDR_TASK_FORCE_REVIEW", called_args[4])
            self.assertIn("%12", called_args)

        # Failure/exception case: state transition must still succeed even if notification throws
        tid2 = "task-fr-notify-02"
        self._create_task(tid2, status="working", version=1, workflow_id=wid)
        with patch("console.common.run", side_effect=Exception("tmux pane unreachable")):
            res2 = task_api.api_task_force_review({"task_id": tid2})
            self.assertFalse(res2.get("notified"))
            self.assertEqual(res2.get("status"), "agent_done")
            task2 = self.store.get_task(tid2)
            self.assertEqual(task2["status"], "agent_done")
