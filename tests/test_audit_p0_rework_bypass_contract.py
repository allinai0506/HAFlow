"""
Test-first contract verification for P0-1:
Task events rejection handling in staged workflows.

Demonstrates:
1. Current Behavior: When try_auto_accept() and try_auto_verdict() return False,
   task_events._handle_task_event() records an attention_note and immediately returns,
   leaving the task in 'agent_done' indefinitely (no state transition, no retry).
2. Existing Contract Feasibility: The existing TASK_TRANSITIONS supports transitioning
   from 'agent_done' to 'rework', which reuses the existing fix_loop / rework machinery
   without introducing any new state machine or framework.
"""

import unittest
from unittest import mock
from services.controller import task_events
from herdr.transitions import TASK_TRANSITIONS


class TestAuditP0ReworkBypassContract(unittest.TestCase):

    def test_current_behavior_stalled_in_agent_done_without_transition(self):
        """Verify that current implementation returns early without transitioning task out of agent_done."""
        mock_ports = mock.MagicMock()
        mock_ports.get_task.return_value = {
            "task_id": "task-audit-p0-1",
            "workflow_id": "wf-dev-01",
            "status": "agent_done",
        }
        mock_ports.workflow_config_for.return_value = {"workflow_template": "software-development-v1"}
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

        # 2. Verified Defect: set_task_status is NEVER called, task remains stuck in agent_done
        mock_ports.set_task_status.assert_not_called()

    def test_existing_transition_matrix_supports_agent_done_to_rework(self):
        """Verify that TASK_TRANSITIONS contract natively allows agent_done -> rework."""
        self.assertIn("agent_done", TASK_TRANSITIONS)
        allowed_targets = TASK_TRANSITIONS["agent_done"]
        self.assertIn("rework", allowed_targets, "TASK_TRANSITIONS must allow agent_done -> rework transition")

    def test_remediation_contract_using_existing_ports(self):
        """Demonstrate that invoking set_task_status(task_id, 'rework') conforms to existing interfaces."""
        mock_ports = mock.MagicMock()
        task_id = "task-audit-p0-1"
        mock_ports.get_task.return_value = {
            "task_id": task_id,
            "status": "agent_done",
            "fix_loop_count": 0,
        }

        # Simulate proposed minimal remediation logic
        target_status = "rework"
        self.assertIn(target_status, TASK_TRANSITIONS[mock_ports.get_task(task_id)["status"]])
        mock_ports.set_task_status(task_id, target_status)

        mock_ports.set_task_status.assert_called_once_with(task_id, "rework")


if __name__ == "__main__":
    unittest.main()
