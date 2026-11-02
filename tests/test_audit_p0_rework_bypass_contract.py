"""
Test-first contract verification for P0-1:
Task events rejection handling in staged workflows and rework dispatch feasibility.

Checks unknown staged rejection escalation, the existing transition matrix,
real SQLite rework delivery, and the separate delivery repair budget.
"""

import os
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from herdr import kernel, task_resources
from herdr.state_store import SQLiteStateStore
from herdr.task_cli_rework import handle_rework
from herdr.transitions import TASK_TRANSITIONS
from services.controller import task_events


class TestAuditP0ReworkBypassContract(unittest.TestCase):

    def test_unknown_failure_escalates_without_transition(self):
        """Unknown failures escalate, retaining the task and coordinator bypass."""
        mock_ports = mock.MagicMock()
        mock_ports.get_task.return_value = {
            "task_id": "task-audit-p0-1",
            "workflow_id": "wf-dev-01",
            "status": "agent_done",
        }
        mock_ports.workflow_config_for.return_value = {"workflow_template": "software-development-v1"}
        mock_ports.attention_escalated.return_value = False
        mock_ports.auto_rework_attempts.return_value = 0
        mock_ports.try_auto_accept.return_value = False
        mock_ports.try_auto_verdict.return_value = False
        mock_ports.verdict_defer_reason.return_value = "deliverable regex mismatch"

        item = {
            "key": "task-audit-p0-1:event",
            "task_id": "task-audit-p0-1",
            "event_type": "done",
            "workflow_id": "wf-dev-01",
        }

        task_events._handle_task_event(mock_ports, item)

        # 1. Verified: attention_note is written
        mock_ports.attention_note.assert_called_once()
        _, kwargs = mock_ports.attention_note.call_args
        self.assertIn("auto_acceptance_rejected", kwargs.get("reason", ""))

        self.assertTrue(kwargs["escalated"])
        self.assertEqual(kwargs["reason_code"], "task_identity_incomplete")
        mock_ports.subprocess.run.assert_not_called()

        # Fail closed without inventing a terminal task status.
        mock_ports.set_task_status.assert_not_called()

    def test_existing_transition_matrix_supports_agent_done_to_rework(self):
        """Verify that TASK_TRANSITIONS contract natively allows agent_done -> rework."""
        self.assertIn("agent_done", TASK_TRANSITIONS)
        allowed_targets = TASK_TRANSITIONS["agent_done"]
        self.assertIn("rework", allowed_targets, "TASK_TRANSITIONS must allow agent_done -> rework transition")

    def test_rework_dispatch_with_real_sqlite_store_and_transport(self):
        """Verify end-to-end rework dispatch using existing handle_rework and real SQLite persistence."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "state.db")
            store = SQLiteStateStore(db_path)
            store.save_workflow({"workflow_id": "wf-dev-01", "name": "dev", "status": "running"})
            store.save_task({
                "task_id": "task-audit-p0-1",
                "workflow_id": "wf-dev-01",
                "run_id": "run-01",
                "pane_id": "101",
                "agent": "codex",
                "stage": "requirements",
                "status": "agent_done",
                "version": 1,
            })

            sent_prompts = []

            def fake_herdr(*args):
                sent_prompts.append(args)
                return SimpleNamespace(returncode=0)

            with mock.patch.object(task_resources, "owned_live_pane", return_value=(True, "match")):
                rework_args = SimpleNamespace(
                    task_id="task-audit-p0-1",
                    request_id="req-audit-456",
                    reason="auto_acceptance_rejected: missing acceptance criteria",
                    prompt="请补充验收标准段落",
                    execution_token=None,
                )
                handle_rework(
                    rework_args,
                    get_store=lambda: store,
                    herdr=fake_herdr,
                    is_gate_task=lambda t: False,
                    recover_unconsumed_gate_cleanup_id=lambda t, s: None,
                    cleanup_gate_for_rework=lambda t, tok, s: None,
                    now=time.time,
                )

            # Assert database state updated
            updated_task = store.get_task("task-audit-p0-1")
            self.assertEqual(updated_task["status"], "rework")
            self.assertEqual(updated_task.get("rework_delivery"), "delivered")
            self.assertEqual(updated_task.get("rework_request_id"), "req-audit-456")

            # Assert transport prompt delivered to Worker pane
            self.assertEqual(len(sent_prompts), 1)
            self.assertEqual(sent_prompts[0][0], "agent")
            self.assertEqual(sent_prompts[0][1], "prompt")
            self.assertEqual(sent_prompts[0][2], "101")
            self.assertIn("请补充验收标准段落", sent_prompts[0][3])

            # Simulate downstream loop completing successfully
            kernel.transition_task("task-audit-p0-1", "working", reason="worker_repairing", source="agent", store=store)
            kernel.transition_task("task-audit-p0-1", "agent_done", reason="worker_done", source="agent", store=store)
            kernel.transition_task("task-audit-p0-1", "completed", reason="auto_accept_pass", source="controller", store=store)

            final_task = store.get_task("task-audit-p0-1")
            self.assertEqual(final_task["status"], "completed")


class TestAuditP0ReworkBudgetExhaustion(unittest.TestCase):

    def test_rework_budget_exhausted_prevents_further_dispatch(self):
        """Verify delivery_rework_attempts >= 3 causes repair_reason() to return 'delivery_repair_budget_exhausted'.

        This guards against the infinite-rework loop that would occur when using the
        non-existent 'auto_rework_count' field (which has no write path in the repository:
        handle_rework() does not increment it, and no other code writes it).

        For the separate delivery repair path the budget field is 'delivery_rework_attempts', which is atomically
        incremented and persisted by herdr/delivery_rework.py::repair_delivery().
        """
        from herdr.delivery_rework import repair_reason

        task_at_limit = {
            "task_id": "task-audit-p0-1",
            "status": "completed",
            "run_id": "run-01",
            "execution_id": "exec-01",
            "workflow_id": "wf-01",
            # writable_task() requires integration_mode='git' and no context/verifier/review role
            "integration_mode": "git",
            "execution_mode": "standard",
            "dispatch_role": "executor",
            "delivery_rework_attempts": 3,  # at budget limit: >= 3 must block further dispatch
            "delivery_failure": {
                "status": "blocked",
                "run_id": "run-01",
                "task_id": "task-audit-p0-1",
                "workflow_id": "wf-01",
                "execution_id": "exec-01",
            },
            "delivery_contract": {
                "version": 1,
                "allowed_paths": ["app.py"],
                "required_files": [],
                "checks": [],
                "auto_rework": True,
            },
            "completion_protocol": None,
        }
        reason = repair_reason(task_at_limit)
        self.assertEqual(
            reason,
            "delivery_repair_budget_exhausted",
            f"Expected budget exhausted guard at attempts=3, got: {reason!r}",
        )

    def test_rework_budget_below_limit_passes_guard(self):
        """Verify delivery_rework_attempts < 3 does NOT trigger budget exhaustion."""
        from herdr.delivery_rework import repair_reason

        task_under_limit = {
            "task_id": "task-audit-p0-1",
            "status": "completed",
            "run_id": "run-01",
            "execution_id": "exec-01",
            "workflow_id": "wf-01",
            # writable_task() requires integration_mode='git' and no context/verifier/review role
            "integration_mode": "git",
            "execution_mode": "standard",
            "dispatch_role": "executor",
            "delivery_rework_attempts": 1,  # below limit
            "delivery_failure": {
                "status": "blocked",
                "run_id": "run-01",
                "task_id": "task-audit-p0-1",
                "workflow_id": "wf-01",
                "execution_id": "exec-01",
            },
            "delivery_contract": {
                "version": 1,
                "allowed_paths": ["app.py"],
                "required_files": [],
                "checks": [],
                "auto_rework": True,
            },
            "completion_protocol": None,
        }
        reason = repair_reason(task_under_limit)
        self.assertNotEqual(
            reason,
            "delivery_repair_budget_exhausted",
            f"Budget should not be exhausted at attempts=1, got: {reason!r}",
        )


if __name__ == "__main__":
    unittest.main()
